import os
import logging
from io import BytesIO

import redis
import requests
from environs import Env
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Filters,
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
)

logger = logging.getLogger(__name__)

START, HANDLE_MENU, HANDLE_CART, HANDLE_QUANTITY = range(4)


def get_database_connection(env):
    return redis.Redis(
        host=env.str('REDIS_HOST'),
        port=env.int('REDIS_PORT'),
        password=env.str('REDIS_PASSWORD'),
        decode_responses=True
    )


def get_products(env):
    strapi_url = env.str('STRAPI_URL')
    strapi_token = env.str('STRAPI_TOKEN')
    headers = {'Authorization': f'Bearer {strapi_token}'}
    params = {'populate': '*'}
    response = requests.get(f'{strapi_url}/api/products', headers=headers, params=params)
    response.raise_for_status()
    return response.json()['data']


def start(update, context):
    """Начальная команда - показывает приветствие и главное меню"""
    try:
        products = get_products(context.bot_data['env'])
        context.user_data['products'] = products

        # Приветственное сообщение
        welcome_text = (
            "🐟 <b>Добро пожаловать в наш рыбный магазин!</b>\n\n"
            "Здесь вы можете выбрать свежую рыбу высочайшего качества.\n"
            "Все товары продаются на вес в килограммах.\n\n"
            "Выберите действие:"
        )

        keyboard = []

        if products:
            keyboard.append([InlineKeyboardButton('🎣 Посмотреть рыбу', callback_data='show_products')])

        keyboard.extend([
            [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')],
            [InlineKeyboardButton('ℹ️ О магазине', callback_data='about')]
        ])

        if update.message:
            update.message.reply_text(
                welcome_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Если вызвано из callback
            query = update.callback_query
            safe_edit_message(
                query, context, welcome_text, keyboard, parse_mode='HTML'
            )

        return HANDLE_MENU
    except Exception as e:
        logger.error(f"Ошибка в start: {e}")
        error_message = '⚠️ Не удалось загрузить данные. Попробуйте позже.'
        if update.message:
            update.message.reply_text(error_message)
        return ConversationHandler.END


def show_products_list(update, context):
    """Показывает список всех продуктов"""
    query = update.callback_query
    query.answer()

    products = context.user_data.get('products', [])
    if not products:
        safe_edit_message(
            query, context,
            '📋 Сейчас нет доступных товаров',
            [[InlineKeyboardButton('⬅️ В главное меню', callback_data='main_menu')]]
        )
        return HANDLE_MENU

    keyboard = [
        [InlineKeyboardButton(f"🐟 {product['title']}", callback_data=str(product['id']))]
        for product in products
    ]
    keyboard.extend([
        [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')],
        [InlineKeyboardButton('⬅️ В главное меню', callback_data='main_menu')]
    ])

    safe_edit_message(
        query, context,
        '🎣 <b>Выберите рыбу:</b>',
        keyboard,
        parse_mode='HTML'
    )
    return HANDLE_MENU


def show_about(update, context):
    """Показывает информацию о магазине"""
    query = update.callback_query
    query.answer()

    about_text = (
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

    safe_edit_message(
        query, context, about_text, keyboard, parse_mode='HTML'
    )
    return HANDLE_MENU


def handle_product_selection(update, context):
    """Обработчик выбора продуктов и навигации в главном меню"""
    query = update.callback_query
    query.answer()

    # Навигационные команды
    if query.data == 'main_menu':
        return start(update, context)
    elif query.data == 'show_products':
        return show_products_list(update, context)
    elif query.data == 'about':
        return show_about(update, context)
    elif query.data == 'view_cart':
        return view_cart(update, context)
    elif query.data == 'back_to_products':
        return show_products_list(update, context)
    elif query.data.startswith('add_'):
        product_id = query.data.split('_')[1]
        return ask_quantity(update, context, product_id)

    # Показ детальной информации о продукте
    product = get_selected_product(query.data, context)
    if not product:
        safe_edit_message(
            query, context,
            '❌ Товар не найден',
            [[InlineKeyboardButton('🎣 К списку рыбы', callback_data='show_products')]]
        )
        return HANDLE_MENU

    show_product_details(query, context, product)
    return HANDLE_MENU


def ask_quantity(update, context, product_id):
    """Запрашивает количество товара для добавления в корзину"""
    query = update.callback_query
    context.user_data['current_product'] = product_id

    product = get_selected_product(product_id, context)
    product_name = product['title'] if product else 'товар'

    text = f"📦 <b>Выберите количество:</b>\n🐟 {product_name}"

    keyboard = [
        [InlineKeyboardButton("0.5 кг", callback_data=f"qty_0.5_{product_id}")],
        [InlineKeyboardButton("1 кг", callback_data=f"qty_1.0_{product_id}")],
        [InlineKeyboardButton("1.5 кг", callback_data=f"qty_1.5_{product_id}")],
        [InlineKeyboardButton("2 кг", callback_data=f"qty_2.0_{product_id}")],
        [InlineKeyboardButton("✏️ Ввести свое количество", callback_data="custom_qty")],
        [InlineKeyboardButton("⬅️ Назад к товару", callback_data=product_id)]
    ]

    safe_edit_message(query, context, text, keyboard, parse_mode='HTML')
    return HANDLE_QUANTITY


def handle_quantity_selection(update, context):
    """Обработчик выбора количества"""
    query = update.callback_query
    query.answer()

    if query.data == 'custom_qty':
        safe_edit_message(
            query, context,
            "✏️ <b>Введите количество в килограммах</b>\n\n"
            "Например: 1.5 или 2.3\n"
            "Минимальное количество: 0.1 кг",
            [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_qty")]],
            parse_mode='HTML'
        )
        return HANDLE_QUANTITY

    if query.data == 'cancel_qty':
        product_id = context.user_data.get('current_product')
        if product_id:
            return ask_quantity(update, context, product_id)
        else:
            return show_products_list(update, context)

    # Обработка кнопок с количеством
    if query.data.startswith('qty_'):
        _, qty, product_id = query.data.split('_')
        return add_to_cart_handler(update, context, product_id, float(qty))

    # Возврат к товару
    product_id = query.data
    product = get_selected_product(product_id, context)
    if product:
        show_product_details(query, context, product)
        return HANDLE_MENU

    return show_products_list(update, context)


def handle_custom_quantity(update, context):
    """Обработчик ввода пользовательского количества"""
    try:
        text = update.message.text.replace(',', '.')  # Поддержка запятой как разделителя
        qty = float(text)

        if qty <= 0:
            update.message.reply_text(
                "❌ Количество должно быть больше 0\n"
                "Введите корректное число (например: 1.5):"
            )
            return HANDLE_QUANTITY

        if qty < 0.1:
            update.message.reply_text(
                "❌ Минимальное количество: 0.1 кг\n"
                "Введите количество от 0.1 кг:"
            )
            return HANDLE_QUANTITY

        if qty > 50:  # Разумное ограничение
            update.message.reply_text(
                "❌ Максимальное количество: 50 кг\n"
                "Введите количество до 50 кг:"
            )
            return HANDLE_QUANTITY

        product_id = context.user_data.get('current_product')
        if not product_id:
            update.message.reply_text("❌ Ошибка: товар не выбран")
            return start(update, context)

        # Удаляем сообщение пользователя
        try:
            context.bot.delete_message(
                chat_id=update.message.chat_id,
                message_id=update.message.message_id
            )
        except Exception:
            pass

        return add_to_cart_handler(update, context, str(product_id), qty)

    except ValueError:
        update.message.reply_text(
            "❌ Неверный формат числа\n"
            "Введите количество в килограммах (например: 1.5):"
        )
        return HANDLE_QUANTITY


def add_to_cart_handler(update, context, product_id_str, quantity=1.0):
    """Добавляет товар в корзину"""
    try:
        product_id = int(product_id_str)
        env = context.bot_data['env']

        if update.callback_query:
            query = update.callback_query
            tg_id = str(query.from_user.id)
            chat_id = query.message.chat_id
        else:
            tg_id = str(update.message.from_user.id)
            chat_id = update.message.chat_id

        # Получаем информацию о продукте
        product = get_selected_product(str(product_id), context)
        if not product:
            raise ValueError("Товар не найден")

        cart = get_cart(env, tg_id) or create_cart(env, tg_id)
        add_to_cart(env, cart['id'], product_id, quantity)

        # Сообщение об успешном добавлении
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

        if update.callback_query:
            safe_edit_message(update.callback_query, context, success_text, keyboard, parse_mode='HTML')
        else:
            context.bot.send_message(
                chat_id=chat_id,
                text=success_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        return HANDLE_MENU

    except Exception as e:
        logger.error(f"Ошибка добавления в корзину: {e}")
        error_text = '❌ Не удалось добавить товар в корзину'

        if update.callback_query:
            update.callback_query.answer(error_text)
            return show_products_list(update, context)
        else:
            update.message.reply_text(error_text)
            return start(update, context)


def view_cart(update, context):
    """Показывает содержимое корзины"""
    query = update.callback_query
    query.answer()

    try:
        tg_id = str(query.from_user.id)
        cart = get_cart(context.bot_data['env'], tg_id)

        if not cart or not cart.get('cart_products'):
            keyboard = [
                [InlineKeyboardButton('🎣 К покупкам', callback_data='show_products')],
                [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
            ]
            safe_edit_message(
                query, context,
                '🛒 <b>Ваша корзина пуста</b>\n\nДобавьте товары из каталога!',
                keyboard,
                parse_mode='HTML'
            )
            return HANDLE_MENU

        # Формируем сообщение с содержимым корзины
        message = "🛒 <b>Ваша корзина:</b>\n\n"
        total = 0

        for item in cart['cart_products']:
            product = item['product']
            item_total = item['quantity'] * product['price']
            message += (
                f"🐟 <b>{product['title']}</b>\n"
                f"📦 {item['quantity']} кг × {product['price']} руб. = {item_total} руб.\n\n"
            )
            total += item_total

        message += f"💵 <b>Итого: {total} руб.</b>"

        # Создаем клавиатуру с кнопками управления
        keyboard = []

        # Кнопки удаления товаров - используем ID из cart_products
        for item in cart['cart_products']:
            # Получаем правильный ID записи cart-product
            cart_product_id = item.get('id')
            if cart_product_id:
                keyboard.append([InlineKeyboardButton(
                    f"❌ Удалить {item['product']['title']}",
                    callback_data=f'remove_{cart_product_id}'
                )])
                logger.info(f"Добавлена кнопка удаления для товара ID: {cart_product_id}")

        # Навигационные кнопки
        keyboard.extend([
            [InlineKeyboardButton('📞 Оформить заказ', callback_data='checkout')],
            [InlineKeyboardButton('🎣 Продолжить покупки', callback_data='show_products')],
            [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
        ])

        safe_edit_message(query, context, message, keyboard, parse_mode='HTML')
        return HANDLE_CART

    except Exception as e:
        logger.error(f"Ошибка просмотра корзины: {e}")
        safe_edit_message(
            query, context,
            '⚠️ Не удалось загрузить корзину',
            [[InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]]
        )
        return HANDLE_MENU


def handle_cart_actions(update, context):
    """Обработчик действий в корзине"""
    query = update.callback_query
    query.answer()

    if query.data == 'main_menu':
        return start(update, context)
    elif query.data == 'show_products':
        return show_products_list(update, context)
    elif query.data == 'checkout':
        return handle_checkout(update, context)
    elif query.data.startswith('remove_'):
        return remove_from_cart(update, context, query.data.split('_')[1])

    return HANDLE_CART


def handle_checkout(update, context):
    """Обработка оформления заказа"""
    query = update.callback_query

    checkout_text = (
        "📞 <b>Оформление заказа</b>\n\n"
        "Для завершения заказа свяжитесь с нами:\n"
        "📱 Телефон: +7 (XXX) XXX-XX-XX\n"
        "💬 Telegram: @your_shop_bot\n\n"
        "Мы свяжемся с вами для подтверждения заказа и согласования доставки!"
    )

    keyboard = [
        [InlineKeyboardButton('🛒 Вернуться в корзину', callback_data='view_cart')],
        [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
    ]

    safe_edit_message(query, context, checkout_text, keyboard, parse_mode='HTML')
    return HANDLE_CART


def remove_from_cart(update, context, item_id):
    """Удаляет товар из корзины с улучшенной обработкой ошибок"""
    query = update.callback_query

    try:
        # Валидация item_id
        try:
            item_id = int(item_id)
        except (ValueError, TypeError):
            logger.error(f"Неверный формат ID товара: {item_id}")
            query.answer('❌ Неверный формат ID товара')
            return HANDLE_CART

        env = context.bot_data['env']
        strapi_url = env.str('STRAPI_URL')
        strapi_token = env.str('STRAPI_TOKEN')
        headers = {'Authorization': f'Bearer {strapi_token}'}

        logger.info(f"Попытка удаления товара с ID: {item_id}")

        # Сначала проверяем, существует ли товар
        check_response = requests.get(
            f'{strapi_url}/api/cart-products/{item_id}',
            headers=headers,
            timeout=10
        )

        if check_response.status_code == 404:
            logger.warning(f"Товар с ID {item_id} не найден")
            query.answer('⚠️ Товар уже удален из корзины')
            return view_cart(update, context)

        # Если товар существует, удаляем его
        delete_response = requests.delete(
            f'{strapi_url}/api/cart-products/{item_id}',
            headers=headers,
            timeout=10
        )

        logger.info(f"Ответ API при удалении: {delete_response.status_code}")

        if delete_response.status_code in [200, 204]:
            query.answer('✅ Товар удален из корзины')
            return view_cart(update, context)
        else:
            logger.error(f"Ошибка API при удалении: {delete_response.status_code}, {delete_response.text}")
            query.answer('❌ Не удалось удалить товар')
            return HANDLE_CART

    except requests.exceptions.Timeout:
        logger.error("Timeout при удалении товара")
        query.answer('❌ Превышено время ожидания. Попробуйте еще раз')
        return HANDLE_CART
    except requests.exceptions.ConnectionError:
        logger.error("Ошибка соединения при удалении товара")
        query.answer('❌ Ошибка соединения. Проверьте интернет')
        return HANDLE_CART
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка сети при удалении: {e}")
        query.answer('❌ Ошибка сети. Попробуйте позже')
        return HANDLE_CART
    except Exception as e:
        logger.error(f"Неожиданная ошибка при удалении: {e}")
        query.answer('❌ Произошла неожиданная ошибка')
        return HANDLE_CART


def get_selected_product(product_id_str, context):
    """Получает продукт по ID"""
    try:
        product_id = int(product_id_str)
    except ValueError:
        return None

    products = context.user_data.get('products', [])
    return next((p for p in products if p['id'] == product_id), None)


def show_product_details(query, context, product):
    """Показывает детальную информацию о продукте"""
    message_text = (
        f"🐟 <b>{product['title']}</b>\n\n"
        f"{product['description']}\n\n"
        f"💵 <b>Цена: {product['price']} руб./кг</b>"
    )

    keyboard = [
        [InlineKeyboardButton('➕ Добавить в корзину', callback_data=f'add_{product["id"]}')],
        [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')],
        [InlineKeyboardButton('⬅️ К списку рыбы', callback_data='back_to_products')],
        [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
    ]

    # Проверяем, есть ли изображение у продукта
    if product.get('image') and product['image'].get('url'):
        try:
            # Удаляем текущее сообщение
            context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
            # Отправляем фото с описанием
            send_product_photo(query, context, product, message_text, InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            # Если не удалось отправить фото, показываем текст
            safe_edit_message(query, context, message_text, keyboard, parse_mode='HTML')
    else:
        safe_edit_message(query, context, message_text, keyboard, parse_mode='HTML')


def safe_edit_message(query, context, text, keyboard, parse_mode=None):
    """Безопасное редактирование сообщения с fallback на отправку нового"""
    try:
        # Проверяем, отличается ли новый контент от текущего
        current_text = query.message.text or query.message.caption or ""
        if current_text.strip() == text.strip():
            # Если текст одинаковый, просто отвечаем на callback без изменений
            return

        query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.warning(f"Не удалось отредактировать сообщение: {e}")
        try:
            # Удаляем старое сообщение
            context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
        except Exception:
            pass

        # Отправляем новое
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


def send_product_photo(query, context, product, caption, reply_markup):
    """Отправляет фото продукта"""
    try:
        base_url = context.bot_data['env'].str('STRAPI_URL').rstrip('/')
        image_path = product['image']['url']

        # Проверяем, начинается ли путь с http (полный URL) или это относительный путь
        if image_path.startswith('http'):
            image_url = image_path
        else:
            # Убираем лишние слеши
            image_path = image_path.lstrip('/')
            image_url = f"{base_url}/{image_path}"

        logger.info(f"Попытка загрузки изображения: {image_url}")

        response = requests.get(image_url, stream=True, timeout=10)
        response.raise_for_status()

        # Проверяем, что это действительно изображение
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            logger.error(f"Неверный тип контента: {content_type}")
            raise ValueError("Файл не является изображением")

        with BytesIO(response.content) as photo_data:
            photo_data.seek(0)
            context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo_data,
                caption=caption,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            logger.info("Изображение успешно отправлено")

    except requests.exceptions.Timeout:
        logger.error("Таймаут при загрузке изображения")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка сети при загрузке изображения: {e}")
        raise
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        raise


def get_cart(env, tg_id):
    """Получает корзину пользователя"""
    strapi_url = env.str('STRAPI_URL')
    strapi_token = env.str('STRAPI_TOKEN')
    headers = {'Authorization': f'Bearer {strapi_token}'}

    params = {
        'filters[tg_id][$eq]': str(tg_id),
        'populate[cart_products][populate][0]': 'product'
    }

    response = requests.get(f'{strapi_url}/api/carts', headers=headers, params=params)
    response.raise_for_status()

    carts = response.json()['data']
    return carts[0] if carts else None


def create_cart(env, tg_id):
    """Создает новую корзину"""
    strapi_url = env.str('STRAPI_URL')
    strapi_token = env.str('STRAPI_TOKEN')
    headers = {
        'Authorization': f'Bearer {strapi_token}',
        'Content-Type': 'application/json'
    }

    data = {
        'data': {
            'tg_id': str(tg_id)
        }
    }

    response = requests.post(f'{strapi_url}/api/carts', headers=headers, json=data)
    response.raise_for_status()
    return response.json()['data']


def add_to_cart(env, cart_id, product_id, quantity=1.0):
    """Добавляет товар в корзину"""
    strapi_url = env.str('STRAPI_URL')
    strapi_token = env.str('STRAPI_TOKEN')
    headers = {
        'Authorization': f'Bearer {strapi_token}',
        'Content-Type': 'application/json'
    }

    data = {
        "data": {
            "quantity": float(quantity),
            "product": int(product_id),
            "cart": int(cart_id)
        }
    }

    try:
        response = requests.post(
            f'{strapi_url}/api/cart-products',
            headers=headers,
            json=data
        )
        response.raise_for_status()
        return response.json()['data']
    except requests.exceptions.HTTPError as e:
        error_msg = e.response.json().get('error', {}).get('message', str(e))
        logger.error(f"Ошибка добавления в корзину: {error_msg}")
        raise ValueError(f"Ошибка сервера: {error_msg}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка: {str(e)}")
        raise


def error_handler(update, context):
    """Обработчик ошибок"""
    logger.error("Ошибка во время обработки обновления:", exc_info=context.error)

    if update and update.effective_chat:
        try:
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text='⚠️ Произошла ошибка. Попробуйте еще раз или начните заново с команды /start'
            )
        except Exception:
            pass


def main():
    """Основная функция запуска бота"""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    try:
        env = Env()
        env.read_env()

        redis_conn = get_database_connection(env)
        redis_conn.ping()

        updater = Updater(env.str('TG_BOT_TOKEN'))
        dispatcher = updater.dispatcher

        dispatcher.bot_data['env'] = env
        dispatcher.bot_data['redis'] = redis_conn

        dispatcher.add_error_handler(error_handler)

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                HANDLE_MENU: [CallbackQueryHandler(handle_product_selection)],
                HANDLE_CART: [CallbackQueryHandler(handle_cart_actions)],
                HANDLE_QUANTITY: [
                    CallbackQueryHandler(handle_quantity_selection),
                    MessageHandler(Filters.text & ~Filters.command, handle_custom_quantity)
                ]
            },
            fallbacks=[CommandHandler('start', start)],
        )

        dispatcher.add_handler(conv_handler)

        logger.info('🐟 Рыбный бот запущен успешно!')
        updater.start_polling()
        updater.idle()

    except redis.exceptions.ConnectionError as e:
        logger.critical(f'❌ Ошибка подключения к Redis: {e}')
    except requests.exceptions.RequestException as e:
        logger.critical(f'❌ Ошибка при работе с API: {e}')
    except Exception as e:
        logger.critical('❌ Фатальная ошибка:', exc_info=True)


if __name__ == '__main__':
    main()
