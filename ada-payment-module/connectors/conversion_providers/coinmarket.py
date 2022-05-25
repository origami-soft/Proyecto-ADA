from enum import Enum
from datetime import date
from urllib.parse import urljoin 
from requests import Session


class CoinMarket(Enum):
    # Urls
    url_production = "https://pro-api.coinmarketcap.com"
    url_sandbox = "https://sandbox-api.coinmarketcap.com"
    # Sandbox api-key
    sandbox_api_key = "b54bcf4d-1bca-4e8e-9a24-22ff2c3d462c"
    # Paths
    price_convertion = "/v2/tools/price-conversion"
    # Currency IDS
    USD = 2781
    EUR = 2790
    ARS = 2821
    ADA = 2010
    BTC = 1
    ETH = 1027


class CoinMarketConversion:
    """CoinMarket API implementation.
    For API details visit https://coinmarketcap.com/api/documentation/v1/
    """
    _session = Session()

    def __init__(self, base_url, api_key, sandbox):
        self.sandbox = sandbox
        self.base_url = base_url
        self.headers = {
            "X-CMC_PRO_API_KEY": f"{api_key}",
            }
        # HTTP Error See https://coinmarketcap.com/api/documentation/v1/#section/Errors-and-Rate-Limits
        raise_for_status_hook = lambda response, **_: response.raise_for_status()
        self._session.hooks['response'] = raise_for_status_hook

    def _get_url(self, url_path):
        return urljoin(self.base_url, url_path)

    def _get_currency_id_from_name(self, currency_name):
        return getattr(CoinMarket, currency_name).value

    def price_conversion(self, amount_currency, convert_currency, amount):
        price_conversion_url = self._get_url(CoinMarket.price_convertion.value)
        amount_id = self._get_currency_id_from_name(amount_currency)
        convert_id = self._get_currency_id_from_name(convert_currency)
        params =  {
            "amount": amount,
            "id": amount_id,
            "convert_id": convert_id,
        }
        response = self._session.get(price_conversion_url, params=params, headers=self.headers)
        response_data = response.json()
        # Data structure is diferent between sandbox and production
        quote_data = response_data[str(convert_id)]["data"] if self.sandbox else response_data["data"]
        return quote_data["quote"][str(convert_id)]  # 'last_updated', price'


def get_coinmarket(api_key=None, sandbox=False):
    url = CoinMarket.url_sandbox.value if sandbox else CoinMarket.url_production.value
    if not api_key and sandbox:
        # Use default `api_key` for CoinMaket
        api_key = CoinMarket.sandbox_api_key.value
    return CoinMarketConversion(url, api_key, sandbox)
