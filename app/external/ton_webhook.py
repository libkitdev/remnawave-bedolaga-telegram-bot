"""Обработчик вебхуков от ton-watcher."""

import hmac
import json
from typing import Any

import structlog
from aiohttp import web

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.payment_service import PaymentService


logger = structlog.get_logger(__name__)


def _verify_bearer_token(request: web.Request) -> bool:
    """Проверяет Bearer-токен в заголовке Authorization."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return False
    token = auth_header[len('Bearer '):]
    expected = settings.TON_WEBHOOK_SECRET or ''
    return hmac.compare_digest(token.encode(), expected.encode())


class TonWebhookHandler:
    def __init__(self, payment_service: PaymentService) -> None:
        self.payment_service = payment_service

    async def handle(self, request: web.Request) -> web.Response:
        if not settings.is_ton_enabled():
            logger.warning('Получен TON webhook, но сервис отключён')
            return web.json_response({'status': 'error', 'reason': 'disabled'}, status=503)

        if not _verify_bearer_token(request):
            logger.warning('TON webhook: неверный Bearer-токен')
            return web.json_response({'status': 'error', 'reason': 'unauthorized'}, status=401)

        try:
            payload: dict[str, Any] = await request.json()
        except json.JSONDecodeError:
            logger.error('TON webhook: некорректный JSON')
            return web.json_response({'status': 'error', 'reason': 'invalid_json'}, status=400)

        async with AsyncSessionLocal() as db:
            try:
                await self.payment_service.process_ton_webhook(db, payload)
                await db.commit()
            except Exception as error:
                logger.error('Ошибка обработки TON webhook', error=error)
                await db.rollback()
                return web.json_response({'status': 'error', 'reason': 'internal_error'}, status=500)

        return web.json_response({'status': 'ok'})

    async def health_check(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                'status': 'ok',
                'service': 'ton_webhook',
                'enabled': settings.is_ton_enabled(),
                'path': settings.TON_WEBHOOK_PATH,
            }
        )

    async def options_handler(self, _: web.Request) -> web.Response:
        return web.Response(
            status=200,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            },
        )
