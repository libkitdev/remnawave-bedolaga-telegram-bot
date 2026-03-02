from datetime import UTC, datetime

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_ton_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.is_ton_enabled():
        await callback.answer('❌ Оплата через TON недоступна', show_alert=True)
        return

    min_rub = settings.TON_MIN_AMOUNT_KOPEKS // 100
    max_rub = settings.TON_MAX_AMOUNT_KOPEKS // 100

    message_lines = [
        '🔷 <b>Пополнение через TON</b>',
        '\n',
        f'Введите сумму пополнения от {min_rub} до {max_rub:,} ₽:'.replace(',', ' '),
        '',
        '⚡ Зачисление после подтверждения транзакции',
        '🔒 Без комиссии',
    ]

    keyboard = get_back_keyboard(db_user.language)

    if settings.is_quick_amount_buttons_enabled():
        from .main import get_quick_amount_buttons

        quick_buttons = await get_quick_amount_buttons(db_user.language, db_user)
        if quick_buttons:
            keyboard.inline_keyboard = quick_buttons + keyboard.inline_keyboard

    await callback.message.edit_text(
        '\n'.join(filter(None, message_lines)),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='ton',
        ton_prompt_message_id=callback.message.message_id,
        ton_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_ton_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_ton_enabled():
        await message.answer('❌ Оплата через TON недоступна')
        return

    min_kopeks = settings.TON_MIN_AMOUNT_KOPEKS
    max_kopeks = settings.TON_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_kopeks:
        min_rub = min_kopeks // 100
        await message.answer(f'Минимальная сумма пополнения: {min_rub} ₽')
        return

    if amount_kopeks > max_kopeks:
        max_rub = max_kopeks // 100
        await message.answer(f'Максимальная сумма пополнения: {max_rub:,} ₽'.replace(',', ' '))
        return

    amount_rubles = amount_kopeks / 100

    payment_service = PaymentService(message.bot)

    result = await payment_service.create_ton_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=f'Пополнение баланса на {amount_rubles:.0f} ₽',
    )

    if not result:
        await message.answer(
            '❌ Не удалось создать TON-инвойс. Возможно, курс TON временно недоступен. Попробуйте позже.'
        )
        await state.clear()
        return

    memo = result['memo']
    amount_ton = result['amount_ton']
    wallet_address = result['wallet_address']
    local_payment_id = result['local_payment_id']
    expires_at_str = result.get('expires_at', '')

    # Формируем время истечения для отображения
    expires_text = ''
    if expires_at_str:
        try:
            ttl_minutes = settings.TON_INVOICE_TTL_MINUTES
            expires_text = f'\n⏱ Действует {ttl_minutes} мин'
        except Exception:
            pass

    details = [
        '🔷 <b>Оплата через TON</b>',
        '',
        f'💰 Сумма к зачислению: {amount_rubles:.0f} ₽',
        f'🔷 К оплате: <b>{amount_ton:.4f} TON</b>',
        '',
        '📋 <b>Инструкция:</b>',
        f'1. Отправьте ровно <b>{amount_ton:.4f} TON</b>',
        '   на адрес:',
        f'   <code>{wallet_address}</code>',
        '',
        '2. В поле <b>комментарий/memo</b> укажите:',
        f'   <code>{memo}</code>',
        '',
        '⚠️ <b>Без комментария зачисление невозможно!</b>',
        expires_text,
    ]

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text='📊 Проверить статус',
                    callback_data=f'check_ton_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]
    )

    state_data = await state.get_data()
    prompt_message_id = state_data.get('ton_prompt_message_id')
    prompt_chat_id = state_data.get('ton_prompt_chat_id', message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:
        logger.warning('Не удалось удалить сообщение с суммой TON', delete_error=delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:
            logger.warning('Не удалось удалить сообщение с запросом суммы TON', delete_error=delete_error)

    await message.answer('\n'.join(filter(None, details)), parse_mode='HTML', reply_markup=keyboard)

    await state.clear()


@error_handler
async def check_ton_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
) -> None:
    try:
        local_payment_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('Некорректный идентификатор платежа', show_alert=True)
        return

    from app.database.crud.ton import get_ton_payment_by_id

    payment = await get_ton_payment_by_id(db, local_payment_id)
    if not payment:
        await callback.answer('Платёж не найден', show_alert=True)
        return

    status = (payment.status or '').lower()

    status_messages = {
        'pending': '⏳ Ожидаем поступление TON',
        'paid': '✅ TON получен, баланс зачислен',
        'expired': '⌛ Время ожидания истекло',
    }

    msg = status_messages.get(status, f'ℹ️ Статус: {status}')

    # Если ещё pending — проверяем не истёк ли
    if status == 'pending' and payment.expires_at:
        now = datetime.now(UTC)
        if payment.expires_at < now:
            msg = '⌛ Время ожидания истекло'

    await callback.answer(msg, show_alert=True)
