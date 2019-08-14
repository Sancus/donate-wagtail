import logging

from django.conf import settings
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.utils.timezone import now
from django.utils.translation import gettext as _
from django.views.generic import FormView, TemplateView

from braintree import ErrorCodes
from dateutil.relativedelta import relativedelta
from wagtail.core.models import Page

from . import constants, gateway
from .exceptions import InvalidAddress
from .forms import (
    BraintreeCardPaymentForm, BraintreePaypalPaymentForm, BraintreePaypalUpsellForm,
    NewsletterSignupForm, StartCardPaymentForm, UpsellForm
)
from .tasks import queue, send_newsletter_subscription_to_basket, send_transaction_to_basket
from .utils import get_currency_info, get_suggested_monthly_upgrade, freeze_transaction_details_for_session

logger = logging.getLogger(__name__)


class BraintreePaymentMixin:
    success_url = reverse_lazy('payments:newsletter_signup')

    def get_custom_fields(self, form):
        return {}

    def get_merchant_account_id(self, currency):
        return settings.BRAINTREE_MERCHANT_ACCOUNTS[currency]

    def get_plan_id(self, currency):
        return settings.BRAINTREE_PLANS[currency]

    def get_transaction_details_for_session(self, result, form, **kwargs):
        raise NotImplementedError()

    def get_source_page_id(self):
        raise NotImplementedError()

    def process_braintree_error_result(result, form):
        raise NotImplementedError()

    def success(self, result, form, send_data_to_basket=True, **kwargs):
        # Store details of the transaction in a session variable
        details = self.get_transaction_details_for_session(result, form, **kwargs)
        source_page_id = self.get_source_page_id()
        details['source_page_id'] = source_page_id
        details['locale'] = self.request.LANGUAGE_CODE
        details = freeze_transaction_details_for_session(details)
        self.request.session['completed_transaction_details'] = details
        self.request.session['source_page_id'] = source_page_id
        if send_data_to_basket:
            queue.enqueue(send_transaction_to_basket, details)
        return HttpResponseRedirect(self.get_success_url())


class CardPaymentView(BraintreePaymentMixin, FormView):
    form_class = BraintreeCardPaymentForm
    template_name = 'payment/card.html'

    def dispatch(self, request, *args, **kwargs):
        if kwargs['frequency'] not in constants.FREQUENCIES:
            raise Http404()
        self.payment_frequency = kwargs['frequency']

        # Ensure that the donation amount and currency are legit
        start_form = StartCardPaymentForm(request.GET)
        if not start_form.is_valid():
            return HttpResponseRedirect('/')

        self.amount = start_form.cleaned_data['amount']
        self.currency = start_form.cleaned_data['currency']
        self.source_page_id = start_form.cleaned_data['source_page_id']
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {
            'amount': self.amount
        }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update({
            'currency_info': get_currency_info(self.currency),
            'braintree_params': settings.BRAINTREE_PARAMS,
            'payment_frequency': self.payment_frequency,
            'gateway_address_errors': getattr(self, 'gateway_address_errors', None),
        })
        return ctx

    def get_address_info(self, form_data):
        address_info = {
            'street_address': form_data['address_line_1'],
            'locality': form_data['town'],
            'postal_code': form_data['post_code'],
            'country_code_alpha2': form_data['country'],
        }

        if address_info.get('region'):
            address_info['region'] = form_data['region']

        return address_info

    def filter_user_card_errors(self, result):
        client_errors = {
            ErrorCodes.CreditCard.CreditCardTypeIsNotAccepted: _('The type of card you used is not accepted.'),
            ErrorCodes.CreditCard.CvvIsInvalid: _('The CVV code you entered was invalid.'),
            ErrorCodes.CreditCard.CvvVerificationFailed: _('The CVV code you entered was invalid.'),
            ErrorCodes.CreditCard.ExpirationDateIsInvalid: _('The expiration date you entered was invalid.'),
            ErrorCodes.CreditCard.NumberIsInvalid: _('The credit card number you entered was invalid.'),
        }
        return [
            client_errors[error.code] for error in result.errors.deep_errors
            if error.code in client_errors.keys()
        ]

    def check_for_address_errors(self, result):
        errors = {
            ErrorCodes.Address.PostalCodeInvalidCharacters: _('The post code you provided is not valid.'),
            ErrorCodes.Address.PostalCodeIsTooLong: _('The post code you provided is not valid.'),
        }
        for error in result.errors.deep_errors:
            if error.code in errors:
                # The view is expected to catch this exception and report the error
                # back to the view so that the use can try to correct them.
                raise InvalidAddress(errors=[errors[error.code]])

    def process_braintree_error_result(self, result, form):
        """
        Parse an error result object from Braintree, and look for errors
        that we can report back to the user. If we find any, add these to the
        form.
        """
        default_error_message = _('Sorry there was an error processing your payment. '
                                  'Please try again later or use a different payment method.')

        if result.errors.deep_errors:
            # Validation errors exist - check if they are meaningful to the user
            try:
                self.check_for_address_errors(result)
            except InvalidAddress as e:
                self.gateway_address_errors = e.errors
                return self.form_invalid(form)

            errors_to_report = self.filter_user_card_errors(result)
            if errors_to_report:
                for error_msg in errors_to_report:
                    form.add_error(None, error_msg)
            else:
                form.add_error(None, default_error_message)
        else:
            # Processor decline or some other exception
            form.add_error(None, default_error_message)

        return self.form_invalid(form)

    def form_valid(self, form, send_data_to_basket=True):
        if self.payment_frequency == constants.FREQUENCY_SINGLE:
            return self.process_single_transaction(form, send_data_to_basket=send_data_to_basket)
        else:
            return self.process_monthly_transaction(form, send_data_to_basket=send_data_to_basket)

    def create_customer(self, form):
        result = gateway.customer.create({
            'first_name': form.cleaned_data['first_name'],
            'last_name': form.cleaned_data['last_name'],
            'email': form.cleaned_data['email'],
            'payment_method_nonce': form.cleaned_data['braintree_nonce'],
            'custom_fields': self.get_custom_fields(form),
            'credit_card': {
                'billing_address': self.get_address_info(form.cleaned_data)
            }
        })

        if not result.is_success:
            logger.error(
                'Failed to create Braintree customer: {}'.format(result.message),
                extra={'result': result}
            )

        return result

    def process_single_transaction(self, form, send_data_to_basket=True):
        # Create a customer and payment method for this customer
        # We vault this customer so that upsell doesn't require further authorization
        result = self.create_customer(form)
        if result.is_success:
            payment_method = result.customer.payment_methods[0]
        else:
            return self.process_braintree_error_result(result, form)

        result = gateway.transaction.sale({
            'amount': form.cleaned_data['amount'],
            'merchant_account_id': self.get_merchant_account_id(self.currency),
            'payment_method_token': payment_method.token,
            'options': {
                'submit_for_settlement': True
            }
        })

        if result.is_success:
            return self.success(
                result,
                form,
                payment_method_token=payment_method.token,
                transaction_id=result.transaction.id,
                settlement_amount=result.transaction.disbursement_details.settlement_amount,
                last_4=result.transaction.credit_card_details.last_4,
                send_data_to_basket=send_data_to_basket,
            )
        else:
            logger.error(
                'Failed Braintree transaction: {}'.format(result.message),
                extra={'result': result}
            )
            return self.process_braintree_error_result(result, form)

    def process_monthly_transaction(self, form, send_data_to_basket=True):
        # Create a customer and payment method for this customer
        result = self.create_customer(form)

        if result.is_success:
            payment_method = result.customer.payment_methods[0]
        else:
            return self.process_braintree_error_result(result, form)

        # Create a subcription against the payment method
        result = gateway.subscription.create({
            'plan_id': self.get_plan_id(self.currency),
            'merchant_account_id': self.get_merchant_account_id(self.currency),
            'payment_method_token': payment_method.token,
            'price': form.cleaned_data['amount'],
        })

        if result.is_success:
            return self.success(
                result,
                form,
                transaction_id=result.subscription.id,
                last_4=payment_method.last_4,
                send_data_to_basket=send_data_to_basket,
            )
        else:
            logger.error(
                'Failed to create Braintree subscription: {}'.format(result.message),
                extra={'result': result}
            )
            return self.process_braintree_error_result(result, form)

    def get_transaction_details_for_session(self, result, form, **kwargs):
        details = form.cleaned_data.copy()
        details.update({
            'transaction_id': kwargs['transaction_id'],
            'settlement_amount': kwargs.get('settlement_amount', None),
            'last_4': kwargs['last_4'],
            'payment_method': constants.METHOD_CARD,
            'currency': self.currency,
            'payment_frequency': self.payment_frequency,
            'payment_method_token': kwargs.get('payment_method_token'),
        })
        return details

    def get_source_page_id(self):
        return self.source_page_id

    def get_success_url(self):
        if self.payment_frequency == constants.FREQUENCY_SINGLE:
            return reverse('payments:card_upsell')
        else:
            return super().get_success_url()


class PaypalPaymentView(BraintreePaymentMixin, FormView):
    form_class = BraintreePaypalPaymentForm
    frequency = None
    template_name = 'payment/paypal.html'       # This is only rendered if we have an error

    def form_valid(self, form, send_data_to_basket=True):
        self.payment_frequency = form.cleaned_data['frequency']
        self.currency = form.cleaned_data['currency']
        self.source_page_id = form.cleaned_data['source_page_id']

        if self.payment_frequency == constants.FREQUENCY_SINGLE:
            result = gateway.transaction.sale({
                'amount': form.cleaned_data['amount'],
                'merchant_account_id': self.get_merchant_account_id(self.currency),
                'custom_fields': self.get_custom_fields(form),
                'payment_method_nonce': form.cleaned_data['braintree_nonce'],
                'options': {
                    'submit_for_settlement': True
                }
            })
        else:
            # Create a customer and payment method for this customer
            result = gateway.customer.create({
                'payment_method_nonce': form.cleaned_data['braintree_nonce'],
                'custom_fields': self.get_custom_fields(form),
            })

            if result.is_success:
                payment_method = result.customer.payment_methods[0]
            else:
                logger.error(
                    'Failed to create Braintree customer: {}'.format(result.message),
                    extra={'result': result}
                )
                return self.process_braintree_error_result(result, form)

            # Create a subcription against the payment method
            result = gateway.subscription.create({
                'plan_id': self.get_plan_id(self.currency),
                'merchant_account_id': self.get_merchant_account_id(self.currency),
                'payment_method_token': payment_method.token,
                'price': form.cleaned_data['amount'],
            })

        if result.is_success:
            return self.success(result, form, send_data_to_basket=send_data_to_basket)
        else:
            logger.error(
                'Failed Braintree transaction: {}'.format(result.message),
                extra={'result': result}
            )
            return self.process_braintree_error_result(result, form)

    def process_braintree_error_result(self, result, form):
        return self.get(self.request)

    def get_transaction_details_for_session(self, result, form, **kwargs):
        if self.payment_frequency == constants.FREQUENCY_SINGLE:
            transaction_id = result.transaction.id
            settlement_amount = result.transaction.disbursement_details.settlement_amount,
        else:
            transaction_id = result.subscription.id
            settlement_amount = None
        return {
            'amount': form.cleaned_data['amount'],
            'settlement_amount': settlement_amount,
            'transaction_id': transaction_id,
            'payment_method': constants.METHOD_PAYPAL,
            'currency': self.currency,
            'payment_frequency': self.payment_frequency,
        }

    def get_source_page_id(self):
        return self.source_page_id

    def get_success_url(self):
        if self.payment_frequency == constants.FREQUENCY_SINGLE:
            return reverse('payments:paypal_upsell')
        else:
            return super().get_success_url()


class TransactionRequiredMixin:

    """
    Mixin that redirects the user to the home page if they try to access a view
    without having completed a payment transaction.
    """
    def dispatch(self, request, *args, **kwargs):
        if not request.session.get('completed_transaction_details'):
            return HttpResponseRedirect('/')
        return super().dispatch(request, *args, **kwargs)

    def get_source_page(self):
        source_page_id = self.request.session.get('source_page_id')
        try:
            return Page.objects.live().get(pk=source_page_id).specific
        except Page.DoesNotExist:
            # This is an edge case where the page has been unpublished/deleted
            # after someone initiated a payment from it.
            pass


class CardUpsellView(TransactionRequiredMixin, BraintreePaymentMixin, FormView):
    form_class = UpsellForm
    success_url = reverse_lazy('payments:newsletter_signup')
    template_name = 'payment/card_upsell.html'

    def dispatch(self, request, *args, **kwargs):
        # Avoid repeat submissions and make sure that the previous transaction was
        # a single card transaction.
        last_transaction = self.request.session['completed_transaction_details']
        if not(
            last_transaction['payment_frequency'] == constants.FREQUENCY_SINGLE
            and last_transaction['payment_method'] == constants.METHOD_CARD
        ):
            return HttpResponseRedirect(self.get_success_url())

        self.suggested_upgrade = get_suggested_monthly_upgrade(
            last_transaction['currency'], last_transaction['amount']
        )
        if self.suggested_upgrade is None:
            return HttpResponseRedirect(self.get_success_url())

        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {
            'amount': self.suggested_upgrade
        }

    def form_valid(self, form, send_data_to_basket=True):
        payment_method_token = self.request.session['completed_transaction_details']['payment_method_token']
        currency = self.request.session['completed_transaction_details']['currency']

        # Create a subcription against the payment method
        start_date = now().date() + relativedelta(months=1)     # Start one month from today
        result = gateway.subscription.create({
            'plan_id': self.get_plan_id(currency),
            'merchant_account_id': self.get_merchant_account_id(currency),
            'payment_method_token': payment_method_token,
            'first_billing_date': start_date,
            'price': form.cleaned_data['amount'],
        })

        if result.is_success:
            return self.success(result, form, currency=currency, send_data_to_basket=send_data_to_basket)
        else:
            logger.error(
                'Failed to create Braintree subscription: {}'.format(result.message),
                extra={'result': result}
            )
            return self.process_braintree_error_result(result, form)

    def get_transaction_details_for_session(self, result, form, **kwargs):
        details = form.cleaned_data.copy()
        details.update({
            'transaction_id': result.subscription.id,
            'payment_method': constants.METHOD_CARD,
            'currency': kwargs['currency'],
            'payment_frequency': constants.FREQUENCY_MONTHLY,
        })
        return details

    def get_source_page_id(self):
        # Return the source page ID that is already set on the session from the original single transaction
        return self.request.session.get('source_page_id')

    def process_braintree_error_result(self, result, form):
        default_error_message = _('Sorry there was an error processing your payment. '
                                  'Please try again later.')
        form.add_error(None, default_error_message)
        return self.form_invalid(form)


class PaypalUpsellView(TransactionRequiredMixin, BraintreePaymentMixin, FormView):
    form_class = BraintreePaypalUpsellForm
    success_url = reverse_lazy('payments:newsletter_signup')
    template_name = 'payment/paypal_upsell.html'

    def dispatch(self, request, *args, **kwargs):
        # Avoid repeat submissions and make sure that the previous transaction was
        # a single card transaction.
        last_transaction = self.request.session['completed_transaction_details']
        if not(
            last_transaction['payment_frequency'] == constants.FREQUENCY_SINGLE
            and last_transaction['payment_method'] == constants.METHOD_PAYPAL
        ):
            return HttpResponseRedirect(self.get_success_url())

        self.suggested_upgrade = get_suggested_monthly_upgrade(
            last_transaction['currency'], last_transaction['amount']
        )
        if self.suggested_upgrade is None:
            return HttpResponseRedirect(self.get_success_url())

        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {
            'currency': self.request.session['completed_transaction_details']['currency'],
            'amount': self.suggested_upgrade
        }

    def form_valid(self, form, send_data_to_basket=True):
        self.currency = form.cleaned_data['currency']

        # Create a customer and payment method for this customer
        result = gateway.customer.create({
            'payment_method_nonce': form.cleaned_data['braintree_nonce'],
            'custom_fields': self.get_custom_fields(form),
        })

        if result.is_success:
            payment_method = result.customer.payment_methods[0]
        else:
            logger.error(
                'Failed to create Braintree customer: {}'.format(result.message),
                extra={'result': result}
            )
            return self.process_braintree_error_result(result, form)

        # Create a subcription against the payment method
        start_date = now().date() + relativedelta(months=1)     # Start one month from today
        result = gateway.subscription.create({
            'plan_id': self.get_plan_id(self.currency),
            'merchant_account_id': self.get_merchant_account_id(self.currency),
            'payment_method_token': payment_method.token,
            'first_billing_date': start_date,
            'price': form.cleaned_data['amount'],
        })

        if result.is_success:
            return self.success(result, form, send_data_to_basket=send_data_to_basket)
        else:
            logger.error(
                'Failed Braintree transaction: {}'.format(result.message),
                extra={'result': result}
            )
            return self.process_braintree_error_result(result, form)

    def get_transaction_details_for_session(self, result, form, **kwargs):
        details = form.cleaned_data.copy()
        details.update({
            'transaction_id': result.subscription.id,
            'payment_method': constants.METHOD_PAYPAL,
            'currency': self.currency,
            'payment_frequency': constants.FREQUENCY_MONTHLY,
        })
        return details

    def get_source_page_id(self):
        # Return the source page ID that is already set on the session from the original single transaction
        return self.request.session.get('source_page_id')

    def process_braintree_error_result(self, result, form):
        default_error_message = _('Sorry there was an error processing your payment. '
                                  'Please try again later.')
        form.add_error(None, default_error_message)
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update({
            'braintree_params': settings.BRAINTREE_PARAMS,
        })
        return ctx


class NewsletterSignupView(TransactionRequiredMixin, FormView):
    form_class = NewsletterSignupForm
    success_url = reverse_lazy('payments:completed')
    template_name = 'payment/newsletter_signup.html'

    def get(self, request, *args, **kwargs):
        # Skip this view if the user is already subscribed
        if request.COOKIES.get('subscribed') == '1':
            return HttpResponseRedirect(self.get_success_url())
        return super().get(request, *args, **kwargs)

    def form_valid(self, form, send_data_to_basket=True):
        if send_data_to_basket:
            data = form.cleaned_data.copy()
            data['source_url'] = self.request.get_full_path()
            # TODO - LANGUAGE_CODE is in the format en-us, and basket expects en-US
            # To address as part of https://github.com/mozilla/donate-wagtail/issues/167
            data['lang'] = self.request.LANGUAGE_CODE
            queue.enqueue(send_newsletter_subscription_to_basket, form.cleaned_data)
        return super().form_valid(form)


class ThankYouView(TransactionRequiredMixin, TemplateView):
    template_name = 'payment/thank_you.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['completed_transaction_details'] = self.request.session['completed_transaction_details']
        return ctx
