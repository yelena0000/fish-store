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

START, HANDLE_MENU = range(2)


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

    product = get_selected_product(query.data, context)
    if not product:
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text='❌ Товар не найден'
        )
        return HANDLE_MENU

    show_product_details(query, context, product)
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
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton('⬅️ Вернуться к списку', callback_data='back_to_list')]
    ])

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
