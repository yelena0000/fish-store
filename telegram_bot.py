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


class StrapiError(Exception):
    """Исключение для ошибок Strapi API."""
    pass


class StrapiClient:
    """Клиент для взаимодействия с API Strapi."""
    def __init__(self, api_url: str, token: str):
        """Инициализирует клиента Strapi."""
        self.api_url = api_url.rstrip('/') + '/api'
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        })

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Выполняет HTTP-запрос к API Strapi."""
        url = f'{self.api_url}/{endpoint}'
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def force_cart_refresh(self, cart_id: int) -> None:
        """Обновляет корзину для синхронизации данных в Strapi."""
        # Обновляем tg_id на то же значение, чтобы Strapi обновил запись
        self._make_request('PUT', f'carts/{cart_id}', json={'data': {}})

    def get_products(self) -> List[Dict]:
        """Возвращает список всех товаров из Strapi."""
        return self._make_request('GET', 'products?populate=*').get('data', [])

    def get_cart(self, tg_id: str) -> Optional[Dict]:
        """Возвращает корзину пользователя по его Telegram ID."""
        params = {
            'filters[tg_id][$eq]': tg_id,
            'populate[cart_products][populate][product]': 'true'
        }
        carts = self._make_request('GET', 'carts', params=params).get('data', [])
        return carts[0] if carts else None

    def create_cart(self, tg_id: str) -> Dict:
        """Создает новую корзину для пользователя."""
        data = {'data': {'tg_id': tg_id}}
        return self._make_request('POST', 'carts', json=data)['data']

    def add_to_cart(self, cart_id: int, product_id: int, quantity: float) -> Dict:
        """Добавляет товар в корзину в Strapi."""
        data = {
            'data': {
                'quantity': quantity,
                'product': product_id,
                'cart': cart_id
            }
        }
        return self._make_request('POST', 'cart-products', json=data)['data']

    def remove_from_cart(self, document_id: str, tg_id: str) -> bool:
        """Удаляет товар из корзины по documentId."""
        try:
            self._make_request('DELETE', f'cart-products/{document_id}')
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return False
            else:
                raise
            
        cart_after = self.get_cart(tg_id)
        if cart_after:
            try:
                self.force_cart_refresh(cart_after['id'])
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    pass
                else:
                    raise
        return True

    def create_order(self, tg_id: str, email: str) -> bool:
        """Создает заказ для пользователя."""
        cart = self.get_cart(tg_id)
        if not cart or not cart.get('cart_products'):
            return False

        total = sum(
            item['quantity'] * item['product']['price']
            for item in cart['cart_products']
        )

        order_data = {
            'data': {
                'email': email,
                'order_status': 'new',
                'total': total,
                'cart_products': [item['id'] for item in cart['cart_products']]
            }
        }

        self._make_request('POST', 'orders', json=order_data)
        return True


def start(update: Update, context) -> int:
    """Показывает главное меню и приветствие."""
    # Удаляем последнее фото, если оно есть
    last_photo_id = context.user_data.pop('last_photo_message_id', None)
    if last_photo_id:
        try:
            chat_id = update.effective_chat.id
            context.bot.delete_message(
                chat_id=chat_id,
                message_id=last_photo_id
            )
        except Exception:
            pass

    products = context.bot_data['strapi'].get_products()
    context.user_data['products'] = products

    keyboard = []
    if products:
        keyboard.append([InlineKeyboardButton('🎣 Посмотреть рыбу', callback_data='show_products')])

    keyboard.extend([
        [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')],
        [InlineKeyboardButton('ℹ️ О магазине', callback_data='about')]
    ])

    text = (
        '🐟 <b>Добро пожаловать в наш рыбный магазин!</b>\n\n'
        'Здесь вы можете выбрать свежую рыбу высочайшего качества.\n'
        'Все товары продаются на вес в килограммах.'
    )

    if update.message:
        update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        safe_edit_message(update.callback_query, context, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

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
        "🚚 Быстрая доставка по всему городу\n"
        "💳 Удобная оплата при получении\n\n"
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

    # Удаляем последнее фото, если оно есть
    last_photo_id = context.user_data.pop('last_photo_message_id', None)
    if last_photo_id:
        try:
            context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=last_photo_id
            )
        except Exception:
            pass

    # Всегда обновляем список товаров из Strapi
    products = context.bot_data['strapi'].get_products()
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
    try:
        # Удаляем старое сообщение, если оно есть
        try:
            context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
        except Exception:
            pass
        base_url = context.bot_data['env'].str('STRAPI_URL').rstrip('/')
        image = product.get('image')
        image_path = None
        if image:
            image_path = image.get('formats', {}).get('small', {}).get('url') or image.get('url')
        if not image_path:
            raise ValueError('Нет изображения для товара')
        if image_path.startswith('http'):
            image_url = image_path
        else:
            image_url = f"{base_url}{image_path}" if image_path.startswith('/') else f"{base_url}/{image_path}"
        response = requests.get(image_url, stream=True, timeout=10)
        response.raise_for_status()
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
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
    except Exception:
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=caption,
            parse_mode='HTML',
            reply_markup=reply_markup
        )


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
            ])
        )
        return HANDLE_MENU

    caption = (
        f"🐟 <b>{product['title']}</b>\n\n"
        f"{product.get('description', '')}\n\n"
        f"💵 <b>Цена: {product['price']} руб./кг</b>"
    )
    keyboard = [
        [InlineKeyboardButton('➕ Добавить в корзину', callback_data=f'add_{product["id"]}')],
        [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')],
        [InlineKeyboardButton('⬅️ К списку рыбы', callback_data='show_products')],
        [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
    ]
    try:
        context.bot.delete_message(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id
        )
    except Exception:
        pass
    send_product_photo(query, context, product, caption, InlineKeyboardMarkup(keyboard))
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
    text = f"📦 <b>Выберите количество:</b>\n🐟 {product['title']}"

    keyboard = [
        [InlineKeyboardButton("0.5 кг", callback_data=f"qty_0.5_{product_id}")],
        [InlineKeyboardButton("1 кг", callback_data=f"qty_1.0_{product_id}")],
        [InlineKeyboardButton("1.5 кг", callback_data=f"qty_1.5_{product_id}")],
        [InlineKeyboardButton("2 кг", callback_data=f"qty_2.0_{product_id}")],
        [InlineKeyboardButton("✏️ Ввести свое количество", callback_data="custom_qty")],
        [InlineKeyboardButton("⬅️ Назад к товару", callback_data=f"product_{product_id}")]
    ]

    safe_edit_message(
        query, context,
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
            "✏️ <b>Введите количество в килограммах</b>\n\n"
            "Например: 1.5 или 2.3\n"
            "Минимальное количество: 0.1 кг",
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
    strapi = context.bot_data['strapi']
    query = getattr(update, 'callback_query', None)
    if query is not None:
        tg_id = str(query.from_user.id)
        chat_id = query.message.chat_id
    else:
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

    cart = strapi.get_cart(tg_id) or strapi.create_cart(tg_id)
    strapi.add_to_cart(cart['id'], int(product_id), quantity)

    success_text = (
        f"✅ <b>Добавлено в корзину:</b>\n"
        f"🐟 {product['title']}\n"
        f"📦 Количество: {quantity} кг\n"
        f"💰 Стоимость: {quantity * product['price']} руб."
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
    # Удаляем старое сообщение, если оно есть
    try:
        context.bot.delete_message(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id
        )
    except Exception:
        pass
    tg_id = str(query.from_user.id)
    strapi = context.bot_data['strapi']
    cart = strapi.get_cart(tg_id)

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
        product_id = product.get('id')
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
        # Для удаления используем documentId первого cart_item из группы
        first_cart_item = group['cart_items'][0]
        keyboard.append([InlineKeyboardButton(
            f"❌ Удалить {product['title']}",
            callback_data=f'remove_{first_cart_item["documentId"]}'
        )])

    message += f"💵 <b>Итого: {total} руб.</b>"

    keyboard.extend([
        [InlineKeyboardButton('📞 Оформить заказ', callback_data='checkout')],
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
    """Обрабатывает действия пользователя с корзиной (удаление, оформление заказа и т.д.)."""
    query = update.callback_query
    query.answer()
    # Удаляем старое сообщение, если оно есть
    try:
        context.bot.delete_message(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id
        )
    except Exception:
        pass
    if query.data == 'main_menu':
        return start(update, context)
    elif query.data == 'show_products':
        return show_products_list(update, context)
    elif query.data == 'checkout':
        return handle_checkout(update, context)
    elif query.data.startswith('remove_'):
        item_id = query.data.split('_')[1]
        strapi = context.bot_data['strapi']
        tg_id = str(query.from_user.id)
        removed = strapi.remove_from_cart(item_id, tg_id)
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
        query, context,
        'Для оформления заказа введите ваш email:',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Отмена', callback_data='main_menu')]])
    )
    return WAITING_EMAIL


def handle_email(update: Update, context) -> int:
    """Обрабатывает ввод email и создает заказ."""
    email = update.message.text.strip()

    if not re.match(r'[^@]+@[^@]+\.[^@]+', email):
        update.message.reply_text('⚠️ Введите корректный email')
        return WAITING_EMAIL

    tg_id = str(update.message.from_user.id)
    strapi = context.bot_data['strapi']

    if strapi.create_order(tg_id, email):
        update.message.reply_text('✅ Заказ оформлен! Мы свяжемся с вами.')
    else:
        update.message.reply_text('⚠️ Не удалось оформить заказ. Попробуйте позже.')

    return start(update, context)


def error_handler(update: Update, context):
    """Обрабатывает все ошибки, возникающие в боте."""
    error = context.error

    if isinstance(error, StrapiError):
        logger.error('Ошибка Strapi: %s', str(error))
        text = '⚠️ Ошибка соединения с сервером. Попробуйте позже.'
    else:
        logger.exception('Непредвиденная ошибка')
        text = '⚠️ Произошла непредвиденная ошибка. Мы уже работаем над исправлением.'

    if update and update.effective_chat:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text
        )

    return start(update, context)


def main():
    """Точка входа: запускает бота и настраивает логирование."""
    env = Env()
    env.read_env()

    logging.basicConfig(level=logging.ERROR)
    logger.setLevel(logging.DEBUG)

    try:
        updater = Updater(env.str('TELEGRAM_TOKEN'))
        dispatcher = updater.dispatcher

        strapi = StrapiClient(env.str('STRAPI_URL'), env.str('STRAPI_TOKEN'))
        dispatcher.bot_data['strapi'] = strapi
        dispatcher.bot_data['env'] = env

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
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
            fallbacks=[CommandHandler('start', start)],
            per_message=False
        )

        dispatcher.add_handler(conv_handler)
        dispatcher.add_error_handler(error_handler)

        updater.start_polling()
        updater.idle()

    except Exception:
        logger.exception('Ошибка при запуске бота')
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logger.exception('Критическая ошибка')
        raise
