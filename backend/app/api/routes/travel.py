"""Authenticated read-only travel sales searches; never book or pay."""
from fastapi import APIRouter, Depends

from ...models.travel import HotelQuery, PoiQuery, TransportQuery
from ...services import flyai, identity
from ...config import get_settings
from ..security import rate_limit, require_api_key

router = APIRouter(prefix='/travel', tags=['旅游平台只读查询'],
                   dependencies=[Depends(require_api_key), Depends(rate_limit), Depends(identity.current_user)])


@router.get('/status')
def status():
    s = get_settings()
    return {'provider': 'fliggy_flyai', 'enabled': s.flyai_enabled,
            'key_configured': bool(s.flyai_api_key.get_secret_value()),
            'client_installed': flyai.BUNDLE.is_file(), 'client_version': flyai.VERSION,
            'ready': flyai.ready(), 'read_only': True}


@router.post('/attractions')
def attractions(body: PoiQuery):
    return flyai.query('attractions', body)


@router.post('/hotels')
def hotels(body: HotelQuery):
    return flyai.query('hotels', body)


@router.post('/flights')
def flights(body: TransportQuery):
    return flyai.query('flights', body)


@router.post('/trains')
def trains(body: TransportQuery):
    return flyai.query('trains', body)
