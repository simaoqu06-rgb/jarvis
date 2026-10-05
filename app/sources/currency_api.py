"""Currency-api (fawazahmed0) : taux quotidiens, sans clé. Deux adresses officielles."""
from app.core.http import SourceError, get_json, to_float

URLS = [
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base}.json",
    "https://latest.currency-api.pages.dev/v1/currencies/{base}.json",
]


async def fx_rates(base: str, symbols: list[str]) -> dict:
    last_error: SourceError | None = None
    for url in URLS:
        try:
            payload = await get_json(url.format(base=base.lower()))
            break
        except SourceError as exc:
            last_error = exc
    else:
        raise last_error or SourceError("aucune adresse ne répond")
    table = payload[base.lower()]
    return {
        "base": base.upper(),
        "date": payload["date"],
        "rates": {code: to_float(table.get(code.lower())) for code in symbols},
    }
