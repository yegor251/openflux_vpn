"""docprovider — carrier document provisioning for OpenFlux VPN channels.

Creates the documents that whitelisted-service transports relay through:
a published Yandex document and a public Mail.ru cloud file, one pair per
VPN channel. Pure provisioning: no tunnels, no users, no orchestration.
"""

from .errors import (
    AllAccountsExhaustedError,
    ApiError,
    CaptchaRequiredError,
    CookiesExpiredError,
    DocProviderError,
    LoginError,
    OpenFluxBinaryError,
    ValidationError,
)
from .links import make_link, parse_link
from .mailru import MailruClient
from .models import (
    ChannelDocs,
    MailruAccount,
    MailruDoc,
    PoolLimits,
    YandexAccount,
    YandexDoc,
    sanitize_name,
)
from .pool import AccountPool
from .validate import (
    check_mailru_public,
    check_yandex_doc,
    validate_mailru_url,
    validate_yandex_url,
)
from .yandex import YandexDocCreator

__version__ = "0.1.0"

__all__ = [
    "AccountPool",
    "AllAccountsExhaustedError",
    "ApiError",
    "CaptchaRequiredError",
    "ChannelDocs",
    "CookiesExpiredError",
    "DocProviderError",
    "LoginError",
    "MailruAccount",
    "MailruClient",
    "MailruDoc",
    "OpenFluxBinaryError",
    "PoolLimits",
    "ValidationError",
    "YandexAccount",
    "YandexDoc",
    "YandexDocCreator",
    "check_mailru_public",
    "check_yandex_doc",
    "make_link",
    "parse_link",
    "sanitize_name",
    "validate_mailru_url",
    "validate_yandex_url",
]
