"""Tests hors ligne du repli : principale, secours, dernière valeur connue."""
import pytest

from app.core import fallback
from app.core.cache import Store
from app.core.fallback import Source, resolve
from app.core.http import SourceError


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback, "store", Store(str(tmp_path / "last.json")))


async def ok(**_):
    return {"value": 1}


async def ko(**_):
    raise SourceError("HTTP 503")


async def test_principale_utilisee():
    result = await resolve("x", "k1", [Source("A", ok, "Données : A"), Source("B", ko)], ttl=60)
    assert result.source == "A" and result.data == {"value": 1}
    assert result.attribution == "Données : A" and result.updated_at and not result.stale


async def test_secours_si_principale_tombe():
    result = await resolve("x", "k2", [Source("A", ko), Source("B", ok)], ttl=60)
    assert result.source == "B" and result.errors == ["A : HTTP 503"]


async def test_cle_absente_passe_au_secours(monkeypatch):
    monkeypatch.delenv("CLE_INEXISTANTE", raising=False)
    result = await resolve("x", "k3", [Source("A", ok, requires_key="CLE_INEXISTANTE"), Source("B", ok)], ttl=60)
    assert result.source == "B"


async def test_derniere_valeur_connue_si_tout_tombe():
    first = await resolve("x", "k4", [Source("A", ok)], ttl=0)
    result = await resolve("x", "k4", [Source("A", ko), Source("B", ko)], ttl=0)
    assert result.stale and result.data == {"value": 1}
    assert result.updated_at == first.updated_at
    assert "dernière valeur connue" in result.message


async def test_message_clair_sans_aucune_valeur():
    result = await resolve("x", "k5", [Source("A", ko)], ttl=60)
    assert result.data is None and result.stale and result.message


async def test_cache_evite_un_nouvel_appel():
    calls = []

    async def counted(**_):
        calls.append(1)
        return 1

    await resolve("x", "k6", [Source("A", counted)], ttl=60)
    await resolve("x", "k6", [Source("A", counted)], ttl=60)
    assert len(calls) == 1


async def test_derniere_valeur_survit_au_redemarrage(tmp_path):
    path = str(tmp_path / "persist.json")
    fallback.store = Store(path)
    await resolve("x", "k7", [Source("A", ok)], ttl=0)
    fallback.store = Store(path)  # simule un redémarrage du serveur
    result = await resolve("x", "k7", [Source("A", ko)], ttl=0)
    assert result.stale and result.data == {"value": 1}
