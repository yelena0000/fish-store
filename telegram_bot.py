import os
import logging
from io import BytesIO

import redis
import requests
from environs import Env
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
)

logger = logging.getLogger(__name__)

START, HANDLE_MENU, HANDLE_CART = range(3)


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
    products = get_products(context.bot_data['env'])
    context.user_data['products'] = products

    if not products:
        update.message.reply_text('ℹ️ Сейчас нет доступных товаров')
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(product['title'], callback_data=str(product['id']))]
        for product in products
    ]

    update.message.reply_text(
        '🎣 Выберите рыбу:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return HANDLE_MENU


def handle_product_selection(update, context):
    query = update.callback_query
    query.answer()

    if query.data == 'back_to_list':
        return back_to_list(update, context)
    elif query.data == 'view_cart':
        return view_cart(update, context)
    elif query.data.startswith('add_'):
        product_id = query.data.split('_')[1]
        return add_to_cart_handler(update, context, product_id)

    product = get_selected_product(query.data, context)
    if not product:
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text='❌ Товар не найден'
        )
        return HANDLE_MENU

    show_product_details(query, context, product)
    return HANDLE_MENU


def add_to_cart_handler(update, context, product_id_str):
    query = update.callback_query
    try:
        product_id = int(product_id_str)
        tg_id = str(query.from_user.id)
        env = context.bot_data['env']

        # Получаем или создаем корзину
        cart = get_cart(env, tg_id) or create_cart(env, tg_id)

        # Добавляем товар в корзину
        add_to_cart(env, cart['id'], product_id)

        query.answer('✅ Товар добавлен в корзину')
    except Exception as e:
        logger.error(f"Ошибка добавления в корзину: {e}")
        query.answer('❌ Не удалось добавить товар')

    # Возвращаемся к товару
    product = get_selected_product(product_id_str, context)
    if product:
        show_product_details(query, context, product)
    return HANDLE_MENU


def view_cart(update, context):
    query = update.callback_query
    query.answer()

    try:
        tg_id = str(query.from_user.id)
        cart = get_cart(context.bot_data['env'], tg_id)

        if not cart or not cart.get('cart_products'):
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text='🛒 Ваша корзина пуста'
            )
            return HANDLE_MENU

        # Формируем сообщение с содержимым корзины
        message = "🛒 Ваша корзина:\n\n"
        total = 0

        for item in cart['cart_products']:
            product = item['product']
            message += f"🐟 {product['title']} - {item['quantity']} кг × {product['price']} руб.\n"
            total += item['quantity'] * product['price']

        message += f"\n💵 Итого: {total} руб."

        # Создаем клавиатуру с кнопками управления
        keyboard = [
            [InlineKeyboardButton('❌ Удалить ' + item['product']['title'],
                                  callback_data=f'remove_{item["id"]}')]
            for item in cart['cart_products']
        ]
        keyboard.append([InlineKeyboardButton('⬅️ Вернуться в меню', callback_data='back_to_menu')])

        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return HANDLE_CART
    except Exception as e:
        logger.error(f"Ошибка просмотра корзины: {e}")
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text='⚠️ Не удалось загрузить корзину'
        )
        return HANDLE_MENU


def get_selected_product(product_id_str, context):
    try:
        product_id = int(product_id_str)
    except ValueError:
        return None

    products = context.user_data.get('products', [])
    return next((p for p in products if p['id'] == product_id), None)


def show_product_details(query, context, product):
    message_text = (
        f"🐟 <b>{product['title']}</b>\n\n"
        f"{product['description']}\n\n"
        f"💵 Цена: {product['price']} руб./кг"
    )

    keyboard = [
        [InlineKeyboardButton('⬅️ Вернуться к списку', callback_data='back_to_list')],
        [InlineKeyboardButton('➕ Добавить в корзину', callback_data=f'add_{product["id"]}')],
        [InlineKeyboardButton('🛒 Моя корзина', callback_data='view_cart')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    context.bot.delete_message(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id
    )

    if product.get('image'):
        send_product_photo(query, context, product, message_text, reply_markup)
    else:
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=message_text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )


def send_product_photo(query, context, product, caption, reply_markup):
    base_url = context.bot_data['env'].str('STRAPI_URL').rstrip('/')
    image_path = product['image']['url'].lstrip('/')
    image_url = f"{base_url}/{image_path}"

    response = requests.get(image_url, stream=True)
    response.raise_for_status()

    with BytesIO(response.content) as photo_data:
        photo_data.seek(0)
        context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=photo_data,
            caption=caption,
            parse_mode='HTML',
            reply_markup=reply_markup
        )


def back_to_list(update, context):
    query = update.callback_query
    query.answer()

    products = context.user_data.get('products', [])
    if not products:
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text='ℹ️ Сейчас нет доступных товаров'
        )
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(product['title'], callback_data=str(product['id']))]
        for product in products
    ]

    context.bot.delete_message(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id
    )

    context.bot.send_message(
        chat_id=query.message.chat_id,
        text='🎣 Выберите рыбу:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return HANDLE_MENU


def get_cart(env, tg_id):
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


def handle_cart_actions(update, context):
    query = update.callback_query
    query.answer()

    if query.data == 'back_to_menu':
        return back_to_list(update, context)
    elif query.data.startswith('remove_'):
        return remove_from_cart(update, context, query.data.split('_')[1])

    return HANDLE_CART


def remove_from_cart(update, context, item_id):
    query = update.callback_query
    try:
        strapi_url = context.bot_data['env'].str('STRAPI_URL')
        strapi_token = context.bot_data['env'].str('STRAPI_TOKEN')
        headers = {'Authorization': f'Bearer {strapi_token}'}

        response = requests.delete(
            f'{strapi_url}/api/cart-products/{item_id}',
            headers=headers
        )
        response.raise_for_status()

        query.answer('✅ Товар удален из корзины')
    except Exception as e:
        logger.error(f"Ошибка удаления из корзины: {e}")
        query.answer('❌ Не удалось удалить товар')

    return view_cart(update, context)


def error_handler(update, context):
    logger.error("Ошибка во время обработки обновления:", exc_info=context.error)

    if update and update.effective_chat:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text='⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.'
        )


def main():
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
                HANDLE_CART: [CallbackQueryHandler(handle_cart_actions)]
            },
            fallbacks=[],
        )

        dispatcher.add_handler(conv_handler)

        logger.info('Бот запущен')
        updater.start_polling()
        updater.idle()

    except redis.exceptions.ConnectionError as e:
        logger.critical(f'Ошибка подключения к Redis: {e}')
    except requests.exceptions.RequestException as e:
        logger.critical(f'Ошибка при работе с API: {e}')
    except Exception as e:
        logger.critical('Фатальная ошибка:', exc_info=True)


if __name__ == '__main__':
    main()
