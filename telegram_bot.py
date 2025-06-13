import logging
import re
from typing import Dict, List, Optional
from io import BytesIO

import requests
from environs import Env
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, Filters
)
from telegram.error import BadRequest

logger = logging.getLogger(__file__)

START, HANDLE_MENU, HANDLE_PRODUCTS, HANDLE_CART, HANDLE_QUANTITY, WAITING_EMAIL = range(6)


def init_strapi_session(api_url: str, token: str) -> requests.Session:
    """Инициализирует сессию для работы с Strapi API."""
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    })
    return session


def force_cart_refresh(session: requests.Session, api_url: str, cart_id: int) -> None:
    """Обновляет корзину для синхронизации данных в Strapi."""
    url = f"{api_url.rstrip('/')}/api/carts/{cart_id}"
    response = session.put(url, json={'data': {}})
    response.raise_for_status()


def get_products(session: requests.Session, api_url: str) -> List[Dict]:
    """Возвращает список всех товаров из Strapi."""
    url = f"{api_url.rstrip('/')}/api/products?populate=*"
    response = session.get(url)
    response.raise_for_status()
    return response.json().get('data', [])


def get_cart(session: requests.Session, api_url: str, tg_id: str) -> Optional[Dict]:
    """Возвращает корзину пользователя по его Telegram ID."""
    url = f"{api_url.rstrip('/')}/api/carts"
    params = {
        'filters[tg_id][$eq]': tg_id,
        'populate[cart_products][populate][product]': 'true'
    }
    response = session.get(url, params=params)
    response.raise_for_status()
    carts = response.json().get('data', [])
    return carts[0] if carts else None


def create_cart(session: requests.Session, api_url: str, tg_id: str) -> Dict:
    """Создает новую корзину для пользователя."""
    url = f"{api_url.rstrip('/')}/api/carts"
    cart_payload = {'data': {'tg_id': tg_id}}
    response = session.post(url, json=cart_payload)
    response.raise_for_status()
    return response.json()['data']


def add_item_to_cart(session: requests.Session, api_url: str, cart_id: int, product_id: int, quantity: float) -> Dict:
    """Добавляет товар в корзину в Strapi."""
    url = f"{api_url.rstrip('/')}/api/cart-products"
    cart_item = {
        'data': {
            'quantity': quantity,
            'product': product_id,
            'cart': cart_id
        }
    }
    response = session.post(url, json=cart_item)
    response.raise_for_status()
    return response.json()['data']


def remove_from_cart(session: requests.Session, api_url: str, document_id: str, tg_id: str) -> bool:
    """Удаляет товар из корзины по documentId."""
    url = f"{api_url.rstrip('/')}/api/cart-products/{document_id}"
    try:
        response = session.delete(url)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return False
        raise

    cart_after = get_cart(session, api_url, tg_id)
    if cart_after and cart_after.get('cart_products'):
        force_cart_refresh(session, api_url, cart_after['id'])
    return True


def create_order(session: requests.Session, api_url: str, tg_id: str, email: str) -> bool:
    """Создает заказ для пользователя."""
    cart = get_cart(session, api_url, tg_id)
    if not cart or not cart.get('cart_products'):
        return False

    total = sum(
        item['quantity'] * item['product']['price']
        for item in cart['cart_products']
    )

    url = f"{api_url.rstrip('/')}/api/orders"
    order_details = {
        'data': {
            'email': email,
            'order_status': 'new',
            'total': total,
            'cart_products': [item['id'] for item in cart['cart_products']]
        }
    }
    response = session.post(url, json=order_details)
    response.raise_for_status()
    return True


def start(update: Update, context) -> int:
    """Показывает главное меню и приветствие."""
    last_photo_id = context.user_data.pop('last_photo_message_id', None)
    if last_photo_id:
        context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=last_photo_id
        )

    session = context.bot_data['strapi_session']
    api_url = context.bot_data['api_url']
    products = get_products(session, api_url)
    context.user_data['products'] = products

    keyboard = []
    if products:
        keyboard.append([
            InlineKeyboardButton('🎣 Посмотреть рыбу', callback_data='show_products')
        ])

    keyboard.extend([
        [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')],
        [InlineKeyboardButton('ℹ️ О магазине', callback_data='about')]
    ])

    text = (
        "🐟 <b>Добро пожаловать в наш магазин рыбы!</b>\n\n"
        "Здесь вы можете выбрать свежую рыбу высочайшего качества.\n"
        "Все товары продаются на вес в килограммах."
    )

    if update.message:
        update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        safe_edit_message(update.callback_query, context, text, reply_markup=InlineKeyboardMarkup(keyboard),
                          parse_mode='HTML')

    return HANDLE_MENU


def safe_edit_message(query, context, text, reply_markup=None, parse_mode=None):
    """Редактирует сообщение или отправляет новое, если нельзя отредактировать."""
    try:
        query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except BadRequest as e:
        err = str(e).lower()
        if (
            "no text in the message to edit" in err
            or "message to edit not found" in err
        ):
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        elif "message is not modified" in err:
            pass
        else:
            raise


def show_about(update: Update, context) -> int:
    """Показывает информацию о магазине."""
    query = update.callback_query
    query.answer()

    text = (
        "ℹ️ <b>О нашем рыбном магазине</b>\n\n"
        "🌊 Мы предлагаем только свежую рыбу высочайшего качества\n"
        "📦 Все товары продаются на вес (в килограммах)\n"
        "Для заказа выберите рыбу из каталога и добавьте в корзину!"
    )

    keyboard = [
        [InlineKeyboardButton('🎣 Посмотреть рыбу', callback_data='show_products')],
        [InlineKeyboardButton('⬅️ В главное меню', callback_data='main_menu')]
    ]

    safe_edit_message(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return HANDLE_MENU


def show_products_list(update: Update, context) -> int:
    """Показывает список всех товаров."""
    query = update.callback_query
    query.answer()

    last_photo_id = context.user_data.pop('last_photo_message_id', None)
    if last_photo_id:
        context.bot.delete_message(
            chat_id=query.message.chat_id,
            message_id=last_photo_id
        )

    session = context.bot_data['strapi_session']
    api_url = context.bot_data['api_url']
    products = get_products(session, api_url)
    context.user_data['products'] = products

    if not products:
        safe_edit_message(
            query, context,
            '📋 Сейчас нет доступных товаров',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ В главное меню', callback_data='main_menu')]])
        )
        return HANDLE_MENU

    keyboard = [
        [InlineKeyboardButton(f"🐟 {product['title']}", callback_data=f"product_{product['id']}")]
        for product in products
    ]
    keyboard.extend([
        [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')],
        [InlineKeyboardButton('⬅️ В главное меню', callback_data='main_menu')]
    ])

    safe_edit_message(
        query, context,
        '🎣 <b>Выберите рыбу:</b>',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return HANDLE_MENU


def send_product_photo(query, context, product, caption, reply_markup):
    """Отправляет фото товара с описанием, удаляет старое сообщение с фото."""
    context.bot.delete_message(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id
    )
    base_url = context.bot_data['env'].str('STRAPI_URL').rstrip('/')
    image = product.get('image')
    image_path = image.get('formats', {}).get('small', {}).get('url') or image.get('url') if image else None
    if not image_path:
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=caption,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        return

    image_url = image_path if image_path.startswith('http') else f"{base_url}/{image_path.lstrip('/')}"
    response = requests.get(image_url, stream=True, timeout=10)
    response.raise_for_status()
    if not response.headers.get('content-type', '').startswith('image/'):
        raise ValueError("File is not an image")

    with BytesIO(response.content) as photo_data:
        photo_data.seek(0)
        msg = context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=photo_data,
            caption=caption,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        context.user_data['last_photo_message_id'] = msg.message_id


def show_product_details(update: Update, context) -> int:
    """Показывает подробную информацию о товаре."""
    query = update.callback_query
    query.answer()
    product_id = query.data.split('_')[1]
    products = context.user_data.get('products', [])
    product = next((p for p in products if str(p['id']) == product_id), None)

    if not product:
        safe_edit_message(
            query, context,
            '❌ Товар не найден',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('🎣 К списку рыбы', callback_data='show_products')]
            ]),
            parse_mode='HTML'
        )
        return HANDLE_MENU

    caption = (
        f"🐟 <b>{product['title']}</b>\n\n"
        f"{product.get('description', '')}\n\n"
        f"💵 <b>Цена: {product['price']} руб./кг</b>"
    )
    keyboard = [
        [InlineKeyboardButton('➕ Добавить в корзину', callback_data=f'add_{product_id}')],
        [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')],
        [InlineKeyboardButton('⬅ К списку рыбы', callback_data='show_products')],
        [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
    ]
    send_product_photo(query, context, product, caption, reply_markup=InlineKeyboardMarkup(keyboard))
    return HANDLE_PRODUCTS


def ask_quantity(update: Update, context, product_id: str) -> int:
    """Запрашивает у пользователя количество товара для добавления в корзину."""
    query = update.callback_query
    query.answer()
    products = context.user_data.get('products', [])
    product = next((p for p in products if str(p['id']) == product_id), None)

    if not product:
        return show_products_list(update, context)

    context.user_data['current_product'] = product_id
    text = (
        f"📦 <b>Выберите количество:</b>\n"
        f"🐟 {product['title']}"
    )

    keyboard = [
        [InlineKeyboardButton("0.5 кг", callback_data=f"qty_0.5_{product_id}")],
        [InlineKeyboardButton("1 кг", callback_data=f"qty_1.0_{product_id}")],
        [InlineKeyboardButton("1.5 кг", callback_data=f"qty_1.5_{product_id}")],
        [InlineKeyboardButton("2 кг", callback_data=f"qty_2.0_{product_id}")],
        [InlineKeyboardButton("✏️ Ввести", callback_data="custom_qty")],
        [InlineKeyboardButton("⬅️ Назад к товару", callback_data=f"product_{product_id}")]
    ]

    safe_edit_message(
        query,
        context,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return HANDLE_QUANTITY


def handle_product_selection(update: Update, context) -> int:
    """Обрабатывает выбор товара и навигацию по меню."""
    query = update.callback_query
    query.answer()

    if query.data == 'main_menu':
        return start(update, context)
    elif query.data == 'show_products':
        return show_products_list(update, context)
    elif query.data == 'about':
        return show_about(update, context)
    elif query.data == 'view_cart':
        return view_cart(update, context)
    elif query.data.startswith('product_'):
        return show_product_details(update, context)
    elif query.data.startswith('add_'):
        product_id = query.data.split('_')[1]
        return ask_quantity(update, context, product_id)
    return HANDLE_MENU


def handle_quantity_selection(update: Update, context) -> int:
    """Обрабатывает выбор количества товара."""
    query = update.callback_query
    query.answer()

    if query.data == 'custom_qty':
        query.edit_message_text(
            text=(
                "✏️ <b>Введите количество в килограммах</b>\n\n"
                "Например: 1.5 или 2.3\n"
                "Минимальное количество: 0.1 кг"
            ),
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_qty")]
            ])
        )
        return HANDLE_QUANTITY

    if query.data == 'cancel_qty':
        product_id = context.user_data.get('current_product')
        if product_id:
            return ask_quantity(update, context, product_id)
        return show_products_list(update, context)

    if query.data.startswith('qty_'):
        _, qty, product_id = query.data.split('_')
        return add_to_cart(update, context, product_id, float(qty))

    return HANDLE_QUANTITY


def handle_custom_quantity(update: Update, context) -> int:
    """Обрабатывает ввод пользовательского количества товара."""
    try:
        qty = float(update.message.text.replace(',', '.'))
    except ValueError:
        update.message.reply_text("❌ Введите число (например: 1.5)")
        return HANDLE_QUANTITY

    if qty <= 0:
        update.message.reply_text("❌ Количество должно быть больше 0. Введите корректное число (например: 1.5)")
        return HANDLE_QUANTITY
    if qty < 0.1:
        update.message.reply_text("❌ Минимальное количество: 0.1 кг. Введите количество от 0.1 кг:")
        return HANDLE_QUANTITY
    if qty > 50:
        update.message.reply_text("❌ Максимальное количество: 50 кг. Введите количество до 50 кг:")
        return HANDLE_QUANTITY

    product_id = context.user_data.get('current_product')
    if not product_id:
        update.message.reply_text("❌ Товар не выбран")
        return start(update, context)

    return add_to_cart(update, context, product_id, qty)


def add_to_cart(update: Update, context, product_id: str, quantity: float) -> int:
    """Добавляет товар в корзину пользователя."""
    session = context.bot_data['strapi_session']
    api_url = context.bot_data['api_url']
    query = getattr(update, 'callback_query', None)
    if query is not None:
        tg_id = str(query.from_user.id)
        chat_id = query.message.chat_id
    else:
        if not hasattr(update.message, 'from_user') or not hasattr(update.message, 'chat_id'):
            update.message.reply_text("Ошибка: Не удалось определить пользователя")
            return HANDLE_MENU
        tg_id = str(update.message.from_user.id)
        chat_id = update.message.chat_id

    products = context.user_data.get('products', [])
    product = next((p for p in products if str(p['id']) == product_id), None)
    if not product:
        error_text = '❌ Товар не найден'
        if query is not None:
            query.answer(error_text)
        else:
            update.message.reply_text(error_text)
        return show_products_list(update, context)

    cart = get_cart(session, api_url, tg_id) or create_cart(session, api_url, tg_id)
    add_item_to_cart(session, api_url, cart['id'], int(product_id), quantity)

    success_text = (
        f"✅ <b>Добавлено в корзину:</b>\n"
        f"🐟 {product['title']}\n"
        f"📦 {quantity} кг\n"
        f"💰 {quantity * product['price']} руб."
    )

    keyboard = [
        [InlineKeyboardButton('🛒 Перейти в корзину', callback_data='view_cart')],
        [InlineKeyboardButton('🎣 Продолжить покупки', callback_data='show_products')],
        [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
    ]

    if query is not None:
        safe_edit_message(
            query,
            context,
            success_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        context.bot.send_message(
            chat_id=chat_id,
            text=success_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return HANDLE_MENU


def view_cart(update: Update, context) -> int:
    """Показывает содержимое корзины пользователя."""
    query = update.callback_query
    query.answer()
    context.bot.delete_message(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id
    )
    tg_id = str(query.from_user.id)
    session = context.bot_data['strapi_session']
    api_url = context.bot_data['api_url']
    cart = get_cart(session, api_url, tg_id)

    if not cart or not cart.get('cart_products'):
        safe_edit_message(
            query, context,
            '🛒 <b>Ваша корзина пуста</b>\n\nДобавьте товары из каталога!',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('🎣 Посмотреть рыбу', callback_data='show_products')],
                [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
            ]),
            parse_mode='HTML'
        )
        return HANDLE_MENU

    grouped_items = {}
    for item in cart['cart_products']:
        product = item['product']
        product_id = str(product.get('id'))
        if product_id not in grouped_items:
            grouped_items[product_id] = {
                'product': product,
                'total_quantity': 0,
                'total_price': 0,
                'cart_items': []
            }
        grouped_items[product_id]['total_quantity'] += item['quantity']
        grouped_items[product_id]['total_price'] += item['quantity'] * product['price']
        grouped_items[product_id]['cart_items'].append(item)

    message = '🛒 <b>Ваша корзина:</b>\n\n'
    total = 0
    keyboard = []

    for product_id, group in grouped_items.items():
        product = group['product']
        total += group['total_price']
        message += (
            f"🐟 <b>{product['title']}</b>\n"
            f"📦 {group['total_quantity']} кг × {product['price']} руб. = {group['total_price']} руб.\n\n"
        )
        first_cart_item = group['cart_items'][0]
        keyboard.append([
            InlineKeyboardButton(
                f"❌ Удалить {product['title']}",
                callback_data=f'remove_{first_cart_item["documentId"]}'
            )
        ])

    message += f"💵 <b>Итого: {total} руб.</b>"

    keyboard.extend([
        [InlineKeyboardButton('📩 Оформить заказ', callback_data='checkout')],
        [InlineKeyboardButton('🎣 Продолжить покупки', callback_data='show_products')],
        [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
    ])

    safe_edit_message(
        query, context,
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return HANDLE_CART


def handle_cart_actions(update: Update, context) -> int:
    """Обрабатывает действия пользователя с корзиной."""
    query = update.callback_query
    query.answer()
    context.bot.delete_message(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id
    )
    if query.data == 'main_menu':
        return start(update, context)
    elif query.data == 'show_products':
        return show_products_list(update, context)
    elif query.data == 'checkout':
        return handle_checkout(update, context)
    elif query.data.startswith('remove_'):
        item_id = query.data.split('_')[1]
        session = context.bot_data['strapi_session']
        api_url = context.bot_data['api_url']
        tg_id = str(query.from_user.id)
        removed = remove_from_cart(session, api_url, item_id, tg_id)
        if not removed:
            query.answer('❌ Не удалось удалить товар')
            return HANDLE_CART
        return view_cart(update, context)
    return HANDLE_CART


def handle_checkout(update: Update, context) -> int:
    """Запрашивает email для оформления заказа."""
    query = update.callback_query
    query.answer()

    safe_edit_message(
        query,
        context,
        'Для оформления заказа введите ваш email:',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Отмена', callback_data='main_menu')]]),
        parse_mode='HTML'
    )
    return WAITING_EMAIL


def handle_email(update: Update, context) -> int:
    """Обрабатывает ввод email и создает заказ."""
    email = update.message.text.strip()

    if not re.match(r'[\w\.-]+@[\w\.-]+\.\w+', email):
        update.message.reply_text('⚠️ Введите корректный email')
        return WAITING_EMAIL

    tg_id = str(update.message.from_user.id)
    session = context.bot_data['strapi_session']
    api_url = context.bot_data['api_url']

    if create_order(session, api_url, tg_id, email):
        update.message.reply_text('✅ Заказ успешно оформлен!')
    else:
        update.message.reply_text('⚠️ Не удалось оформить заказ. Попробуйте позже.')

    return start(update, context)


def error_handler(update: Update, context) -> None:
    """Обрабатывает ошибки, возникающие в боте."""
    error = context.error
    logger.error('Ошибка в боте: %s', str(error))
    text = '⚠️ Произошла ошибка. Попробуйте позже.'

    if update and update.effective_chat:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text
        )

    return start(update, context)


def main():
    """Запускает бота и настраивает логирование."""
    logging.basicConfig(level=logging.INFO)
    logger.setLevel(logging.DEBUG)

    env = Env()
    env.read_env()

    updater = Updater(token=env.str('TELEGRAM_TOKEN'))
    dispatcher = updater.dispatcher

    api_url = env.str('STRAPI_URL')
    strapi_session = init_strapi_session(api_url=api_url, token=env.str('STRAPI_TOKEN'))
    dispatcher.bot_data['strapi_session'] = strapi_session
    dispatcher.bot_data['api_url'] = api_url
    dispatcher.bot_data['env'] = env

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
        ],
        states={
            HANDLE_MENU: [
                CallbackQueryHandler(handle_product_selection),
            ],
            HANDLE_PRODUCTS: [
                CallbackQueryHandler(handle_product_selection),
            ],
            HANDLE_QUANTITY: [
                CallbackQueryHandler(handle_quantity_selection),
                MessageHandler(Filters.text & ~Filters.command, handle_custom_quantity),
            ],
            HANDLE_CART: [
                CallbackQueryHandler(handle_cart_actions),
            ],
            WAITING_EMAIL: [
                MessageHandler(Filters.text & ~Filters.command, handle_email),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
        },
        fallbacks=[
            CommandHandler('start', start),
        ],
        per_message=False
    )

    dispatcher.add_handler(conv_handler)
    dispatcher.add_error_handler(error_handler)

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logger.exception('Критическая ошибка')
        raise
