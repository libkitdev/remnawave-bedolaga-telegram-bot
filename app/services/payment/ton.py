"""Mixin с реализацией TON-оплаты через ton-watcher."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TonPayment, TransactionType
from app.services.subscription_auto_purchase_service import auto_purchase_saved_cart_after_topup
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


_NANO = 1_000_000_000


class TonPaymentMixin:
    """Создание TON-платежей и обработка вебхуков от ton-watcher."""

    async def create_ton_payment(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
    ) -> dict[str, Any] | None:
        ton_price_service = getattr(self, 'ton_price_service', None)
        if ton_price_service is None:
            logger.error('TonPriceService не инициализирован')
            return None

        if amount_kopeks <= 0:
            logger.error('Сумма TON должна быть положительной', amount_kopeks=amount_kopeks)
            return None

        amount_rubles = amount_kopeks / 100
        amount_nano = await ton_price_service.rub_to_nano(amount_rubles)
        if amount_nano is None or amount_nano <= 0:
            logger.error('Не удалось конвертировать сумму в нанотоны', amount_rubles=amount_rubles)
            return None

        memo = f'ton_{user_id}_{secrets.token_hex(6)}'

        ttl = settings.TON_INVOICE_TTL_MINUTES
        expires_at = datetime.now(UTC) + timedelta(minutes=ttl)

        ton_crud = import_module('app.database.crud.ton')
        payment = await ton_crud.create_ton_payment(
            db,
            user_id=user_id,
            memo=memo,
            amount_kopeks=amount_kopeks,
            amount_nano=amount_nano,
            expires_at=expires_at,
            metadata={'description': description, 'created_at': datetime.now(UTC).isoformat()},
        )

        amount_ton = amount_nano / _NANO

        logger.info(
            'Создан TON платёж: memo= amount_ton= для пользователя',
            memo=memo,
            amount_ton=amount_ton,
            user_id=user_id,
        )

        return {
            'local_payment_id': payment.id,
            'memo': memo,
            'amount_ton': amount_ton,
            'amount_nano': amount_nano,
            'amount_kopeks': amount_kopeks,
            'wallet_address': settings.TON_WALLET_ADDRESS,
            'expires_at': expires_at.isoformat(),
        }

    async def process_ton_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        if not isinstance(payload, dict):
            logger.error('TON webhook payload не является словарём')
            return False

        # Проверяем успешность транзакции
        if not payload.get('success'):
            logger.info('TON вебхук: транзакция неуспешна, пропускаем')
            return True  # Не ошибка сервера

        # Извлекаем memo из payload
        try:
            memo = payload['transaction_data']['in_msg']['decoded_body']['text']
        except (KeyError, TypeError):
            logger.info('TON вебхук: memo не найден в payload, пропускаем')
            return True  # Неизвестная транзакция — не ошибка

        if not isinstance(memo, str) or not memo.startswith('ton_'):
            logger.info('TON вебхук: memo не совпадает с нашим форматом', memo=memo)
            return True

        # Получаем сумму в нанотонах
        try:
            received_nano = int(payload['transaction_data']['in_msg']['value'])
        except (KeyError, TypeError, ValueError):
            logger.error('TON вебхук: не удалось извлечь сумму', payload=payload)
            return False

        ton_crud = import_module('app.database.crud.ton')

        # Ищем платёж по memo с блокировкой строки для предотвращения гонки
        locked_result = await db.execute(
            select(TonPayment).where(TonPayment.memo == memo).with_for_update()
        )
        payment = locked_result.scalar_one_or_none()

        if not payment:
            logger.info('TON вебхук: платёж с таким memo не найден', memo=memo)
            return True  # Чужая транзакция — не ошибка

        # Проверяем статус
        if payment.status != 'pending':
            logger.info('TON вебхук: платёж не в статусе pending', memo=memo, status=payment.status)
            return True

        # Проверяем не истёк ли платёж
        now = datetime.now(UTC)
        if payment.expires_at and payment.expires_at < now:
            await ton_crud.update_ton_payment(db, payment, status='expired')
            logger.info('TON вебхук: платёж истёк', memo=memo)
            return True

        # Идемпотентность: уже привязан к транзакции
        if payment.transaction_id is not None:
            logger.info('TON вебхук: платёж уже обработан (transaction_id)', memo=memo)
            return True

        # Дополнительная проверка идемпотентности через external_id транзакции
        payment_module = import_module('app.services.payment_service')
        get_transaction_by_external_id = getattr(payment_module, 'get_transaction_by_external_id', None)
        if get_transaction_by_external_id:
            try:
                existing = await get_transaction_by_external_id(db, payment.memo, PaymentMethod.TON)
                if existing:
                    logger.info('TON вебхук: транзакция уже существует по external_id', memo=memo)
                    return True
            except Exception as error:
                logger.warning('TON вебхук: ошибка проверки external_id', error=error)

        # Проверяем хеш транзакции на дубликат
        ton_hash = str(payload.get('hash', ''))[:64] or None
        if ton_hash:
            existing_by_hash = await db.execute(
                select(TonPayment).where(TonPayment.ton_hash == ton_hash)
            )
            if existing_by_hash.scalar_one_or_none():
                logger.warning('TON вебхук: транзакция с таким hash уже обработана', ton_hash=ton_hash)
                return True

        # Проверяем сумму с допуском TON_MIN_AMOUNT_RATIO
        expected_nano = payment.amount_nano
        ratio = settings.TON_MIN_AMOUNT_RATIO
        if received_nano < expected_nano * ratio:
            logger.warning(
                'TON вебхук: недостаточная сумма',
                memo=memo,
                received_nano=received_nano,
                expected_nano=expected_nano,
                ratio=ratio,
            )
            return True  # Не ошибка сервера

        result = await self._finalize_ton_payment(db, payment, payload, ton_hash=ton_hash)
        return result

    async def _finalize_ton_payment(
        self,
        db: AsyncSession,
        payment: Any,
        payload: dict[str, Any],
        *,
        ton_hash: str | None = None,
    ) -> bool:
        ton_crud = import_module('app.database.crud.ton')
        payment_module = import_module('app.services.payment_service')

        paid_at = datetime.now(UTC)

        # Обновляем запись платежа
        payment = await ton_crud.update_ton_payment(
            db,
            payment,
            status='paid',
            paid_at=paid_at,
            ton_hash=ton_hash,
            metadata={'webhook_payload': payload},
        )

        amount_kopeks = payment.amount_kopeks
        if amount_kopeks <= 0:
            logger.error('TON платёж имеет некорректную сумму', memo=payment.memo)
            return False

        # Создаём транзакцию в нашей БД
        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=amount_kopeks,
            description='Пополнение через TON',
            payment_method=PaymentMethod.TON,
            external_id=payment.memo,
            is_completed=True,
            created_at=payment.created_at,
        )

        # Привязываем платёж к транзакции
        await ton_crud.link_ton_payment_to_transaction(db, payment, transaction.id)

        # Зачисляем баланс
        get_user_by_id = payment_module.get_user_by_id
        user = await get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для TON платежа', user_id=payment.user_id)
            return False

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += amount_kopeks
        user.updated_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(user)

        # Реферальное начисление
        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка реферального начисления TON', error=error)

        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        user = await get_user_by_id(db, user.id) or user

        if getattr(self, 'bot', None):
            topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'
            referrer_info = format_referrer_info(user)
            subscription = getattr(user, 'subscription', None)
            promo_group = user.get_primary_promo_group()

            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error('Ошибка отправки админ-уведомления TON', error=error)

            if user.telegram_id:
                try:
                    keyboard = await self.build_topup_success_keyboard(user)
                    amount_ton = payment.amount_nano / _NANO

                    message_lines = [
                        '✅ <b>Пополнение успешно!</b>',
                        f'💰 Сумма: {settings.format_price(amount_kopeks)}',
                        f'🔷 Оплата: {amount_ton:.4f} TON',
                        '💳 Способ: TON',
                    ]

                    await self.bot.send_message(
                        chat_id=user.telegram_id,
                        text='\n'.join(message_lines),
                        parse_mode='HTML',
                        reply_markup=keyboard,
                    )
                except Exception as error:
                    logger.error('Ошибка отправки уведомления пользователю TON', error=error)
            else:
                logger.info('Пропуск Telegram-уведомления TON для email-пользователя', user_id=user.id)

        # Автопокупка из сохранённой корзины
        try:
            from app.services.user_cart_service import user_cart_service

            if await user_cart_service.has_user_cart(user.id):
                try:
                    await auto_purchase_saved_cart_after_topup(
                        db,
                        user,
                        bot=getattr(self, 'bot', None),
                    )
                except Exception as auto_error:
                    logger.error(
                        'Ошибка автоматической покупки подписки TON',
                        user_id=user.id,
                        auto_error=auto_error,
                        exc_info=True,
                    )
        except Exception as error:
            logger.error('Ошибка при автоактивации TON', user_id=user.id, error=error, exc_info=True)

        logger.info(
            'TON платёж финализирован',
            memo=payment.memo,
            amount_kopeks=amount_kopeks,
            user_id=user.id,
        )
        return True
