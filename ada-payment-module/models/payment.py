# coding: utf-8
import logging
import json

from werkzeug import urls

import logging

from odoo import _, api, fields, models
from odoo.addons.payment.models.payment_acquirer import ValidationError
from odoo.tools.float_utils import float_compare
from ..connectors.currency_conversion import COINMARKET_PROVIDER
from ..connectors.conversion_providers.coinmarket import CoinMarket
from ..connectors.currency_conversion import get_conversion_provider
from .utils import generate_b64_qr_image
from ..connectors.adapay import (
    get_adapay_api,
    ada_to_lovelace,
    lovelace_to_ada,
)
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

ADA = "ADA"


class AcquirerAdapay(models.Model):
    _inherit = 'payment.acquirer'

    provider = fields.Selection(selection_add=[
        ('adapay', 'Adapay')
    ], ondelete={'adapay': 'set default'})
    adapay_api_key = fields.Char("AdaPay APIKEY")
    adapay_expiration_minutes = fields.Integer("Payment request expiration (minutes)", default=15)
    adapay_use_webhook = fields.Boolean("Use WebHook",  # TODO: default False, move to configuration section
        default=False,
        help="Disable update via webhook and do it via cron.")
    conversion_provider = fields.Selection([
            (COINMARKET_PROVIDER, 'CoinMarket')
        ],
        default=COINMARKET_PROVIDER, required=True)
    conversion_api_key = fields.Char("Api-Key")
    conversion_test_mode = fields.Boolean("Test Mode", default=False)

    def _get_conversion_provider(self, data):
        return get_conversion_provider(**data)

    def _get_adapay_api_connector(self):
        sandbox = self.state == "test"
        return get_adapay_api(self.adapay_api_key, sandbox)

    def adapay_form_generate_values(self, values):
        # Perform currency amount conversion
        try:
            currency_converter = self._get_conversion_provider(
                {"provider": self.conversion_provider, "api_key": self.conversion_api_key, "sandbox": self.conversion_test_mode })
            resp = currency_converter.price_conversion(
                amount=values["amount"],
                amount_currency=values["currency"].name,
                convert_currency=ADA,
            )
            adapay_amount = {
                "ada_amount": int(resp["price"]),
                "last_updated": resp["last_updated"],
                "ada_currency": ADA,
            }
            _logger.info(f"{self.conversion_provider}: {values['amount']} {values['currency'].name} -> {resp['price']} {ADA}. Last updated: {resp['last_updated']}")
            values.update(adapay_amount)
        except Exception as err:
            _logger.error("Conversion error: %s", err)
            raise ValidationError("We cannot get the ADA currency rate. Please try again.")
        # Perform adapay payment requests
        try:
            sandbox = self.state == "test"
            adapay_conn = get_adapay_api(self.adapay_api_key, sandbox)
            payment_uuid = adapay_conn.create_payment(
                amount=values["ada_amount"],
                expiration_minutes=self.adapay_expiration_minutes,
                receipt_email=values["partner_email"],
                description=values["reference"],
                name=values["reference"],
                order_id=values["reference"],
            )
            payment_data = adapay_conn.get_payment_by_uuid(payment_uuid["uuid"])
            payment_data = {f"adapay_{key}":value for key, value in payment_data.items()}
            values.update(payment_data)
            values["adapay_expiration_minutes"] = self.adapay_expiration_minutes
        except Exception:
            raise ValidationError("We cannot create the AdaPay payment request. Please try again.")
        return values

    def adapay_get_form_action_url(self):
        self.ensure_one()
        return '/payment/adapay/accept'


class TransactionAdapay(models.Model):
    _inherit = 'payment.transaction'

    adapay_uuid = fields.Char("Payment UUID")
    adapay_amount = fields.Float("Amount in ADA")
    adapay_total_amount_sent = fields.Float("Amount sent")
    adapay_total_amount_left = fields.Float("Amount left")
    adapay_expiration_minutes = fields.Integer("Expiration minutes")
    adapay_expiration_date = fields.Char("Expiration date")
    adapay_update_time = fields.Char("Update time")
    adapay_last_updated = fields.Char("Last updated")
    adapay_status = fields.Char("Payment status", default="new")
    adapay_substatus = fields.Char("Payment sub-status", default="")
    adapay_address = fields.Char("Payment address")
    adapay_transaction_history = fields.Text("Transaction history", default='{}')

    @api.model
    def _adapay_form_get_tx_from_data(self, data, field="reference"):
        reference = data.get("reference")
        tx = self.search([(field, '=', reference)])
        if not tx or len(tx) > 1:
            error_msg = _('received data for reference %s') % (reference)
            if not tx:
                error_msg += _('; no order found')
            else:
                error_msg += _('; multiple order found')
            _logger.info(error_msg)
            raise ValidationError(error_msg)
        return tx

    def _adapay_form_get_invalid_parameters(self, data):
        invalid_parameters = []
        # TODO: restore validation
        # if float_compare(float(data.get('amount') or '0.0'), self.amount, 2) != 0:
        #    invalid_parameters.append(('amount', data.get('amount'), '%.2f' % self.amount))
        if data.get('currency') != self.currency_id.name:
            invalid_parameters.append(('currency', data.get('currency'), self.currency_id.name))
        return invalid_parameters

    def _adapay_form_validate(self, data):
        _logger.info('Validated adapay payment for tx %s: set as pending' % (self.reference))
        data["adapay_amount"] = lovelace_to_ada(data["adapay_amount"])
        # Update tx `adapay_` fields
        adapay_data = {key: value for key, value in data.items() if key.startswith("adapay_")}
        self.write(adapay_data)
        for sale in self.sale_order_ids:
            sale.message_post(body=f"AdaPay payment request created with data: {adapay_data}")
        self._set_transaction_pending()
        return True

    def _process_payment_data(self, payment_data):
        update_date = payment_data["updateTime"]
        if update_date != self.adapay_last_updated:  # Update if it's neccessary
            _logger.info("Received payment update for %s", self.reference)
            self._adapay_webhook_feedback(payment_data)
        for transaction in payment_data["transactions"]:
            self._adapay_webhook_transaction_feedback(transaction)

    def _get_processing_info(self):
        qr_encoded = generate_b64_qr_image(self.adapay_address)
        res = super()._get_processing_info()
        title = {  # TODO: add as attribute
            "new": "To complete your payment, please send ADA to the address below.",
            "payment-sent": "Your payment was received! You payment is pending for confirmation.",
            "pending": "The transaction is in the process of being confirmed.",
            "confirmed": "The transaction is confirmed!",
            "expired": "The payment request is expired. Please back to payment selection."
        }
        if self.acquirer_id.provider == 'adapay':
            # Update status
            if not self.acquirer_id.adapay_use_webhook:
                # We need to update status every time this method is called.
                try: 
                    _logger.info("Going to look for the update for %s", self.reference)
                    conn = self.acquirer_id._get_adapay_api_connector()
                    payment_data = conn.get_payment_by_uuid(self.adapay_uuid)
                    self._process_payment_data(payment_data)
                except Exception as err:
                    _logger.warning("Could not update payment status: %s", err)
            # Render AdaPay payment pages
            exp_time = 0
            if self.adapay_status == 'new':  # Timer, set expiration from backend
                now = datetime.now()
                expiration = self.create_date + timedelta(minutes=self.adapay_expiration_minutes)
                if now > expiration:
                    self.adapay_status = "expired"
                else: 
                    exp_time = (expiration - now).seconds
            transaction_adapay_data = {field:getattr(self, field) for field in self._fields.keys() if field.startswith("adapay")}
            transaction_adapay_data['qr_code'] = f"data:image/png;base64,{qr_encoded}"
            transaction_adapay_data['adapay_transaction_history'] = list(json.loads(self.adapay_transaction_history).values())
            transaction_adapay_data['title'] = title[self.adapay_status]
            payment_frame = self.env.ref("ada-payment-module.payment_adapay_page")._render(transaction_adapay_data, engine="ir.qweb")
            adapay_info = {
                "message_to_display": payment_frame,
                "adapay_expiration_seconds": exp_time,
            }
            if self.adapay_status != "confirmed":  # Prevent confirmation page if payment was not started
                adapay_info["return_url"] = ""
            res.update(adapay_info)
        return res

    def _handle_adapay_webhook(self, data):
        """"""
        event_type = data.get("event")
        data = data.get("data", {})
        if event_type == 'paymentRequestUpdate':
            _logger.info("Webhook: Received a paymentRequestUpdate for %s", data["orderId"])
            tx_reference = data.get("orderId")
            data_tx = {'reference': tx_reference}
            try:
                odoo_tx = self.env['payment.transaction']._adapay_form_get_tx_from_data(data_tx)
            except ValidationError as e:
                _logger.error('Received notification for tx %s: %s', tx_reference, e)
                return False
            odoo_tx._adapay_webhook_feedback(data)
        elif event_type == 'paymentRequestTransactionsUpdate':
            _logger.info("Webhook: Received a paymentRequestUpdate for uuid %s and transaction %s", data["paymentRequestUuid"], data["hash"])
            tx_reference = data.get("paymentRequestUuid")
            data_tx = {'reference': tx_reference}
            try:
                odoo_tx = self.env['payment.transaction']._adapay_form_get_tx_from_data(data_tx, field="adapay_uuid")
            except ValidationError as e:
                _logger.error('Received notification for tx %s: %s', tx_reference, e)
                return False
            odoo_tx._adapay_webhook_transaction_feedback(data)
        return True

    def _adapay_webhook_feedback(self, data):
        status = data["status"]
        # Update data
        self.write({
            "adapay_total_amount_sent": lovelace_to_ada(data["confirmedAmount"]),
            "adapay_total_amount_left": lovelace_to_ada(data["pendingAmount"]),
            "adapay_last_updated": data["updateTime"],
            "adapay_status": status,
            "adapay_substatus": data["subStatus"],
        })
        for sale in self.sale_order_ids:
            sale.message_post(body=f"AdaPay payment update with data: {data}")
        if status == "confirmed":
            if self.state != "done":
                self._set_transaction_done()
        elif status in ["new", "payment-sent", "pending"]:
            if self.state != "pending":
                self._set_transaction_pending()
        else:
            if self.state != "cancel":
                self._set_transaction_cancel()
        return True

    def _adapay_webhook_transaction_feedback(self, data):
        try:
            # Load json structure from string field
            transaction_history = json.loads(self.adapay_transaction_history)
        except json.decoder.JSONDecodeError:
            transaction_history = {}
        transaction_hash = data["hash"]
        if transaction_hash not in transaction_history or \
                data["updateTime"] != transaction_history[transaction_hash]["updateTime"]:
            data["amount"] = lovelace_to_ada(data["amount"])
            transaction_history[transaction_hash] = data
            self.adapay_transaction_history = json.dumps(transaction_history)
            # Update data
            for sale in self.sale_order_ids:
                sale.message_post(body=f"AdaPay transaction update with data: {data}")
        return True

    def _adapay_update_payment_schedule(self):
        _logger.info("Starting AdaPay payments synchronization")
        adapay_acquirer = self.env.ref("ada-payment-module.payment_acquirer_adapay")
        if not adapay_acquirer.adapay_use_webhook:
            adapay_conn = adapay_acquirer._get_adapay_api_connector()
            payments_data = adapay_conn.get_payment()  #TODO: Get the last payments
            for payment in payments_data["data"]:
                transaction = self.search([("adapay_uuid", "=", payment["uuid"])])
                if transaction and transaction.state != 'done':  # Don't process finished payments
                    _logger.info("Processing payment synchronization update for %s", transaction.reference)
                    transaction._process_payment_data(payment)
