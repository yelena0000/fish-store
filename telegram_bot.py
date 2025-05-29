import os
import logging

import redis
import requests
from environs import Env
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler

logger = logging.getLogger(__name__)


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
    response = requests.get(f'{strapi_url}/products', headers=headers)
    response.raise_for_status()
    return response.json()['data']


def start(update, context):
    products = get_products(context.bot_data['env'])
    context.user_data['products'] = products  # Сохраняем все товары

    if not products:
        update.message.reply_text('ℹ️ Сейчас нет доступных товаров')
        return

    keyboard = [
        [InlineKeyboardButton(
            product['title'],
            callback_data=str(product['id'])
        )]
        for product in products
    ]

    update.message.reply_text(
        '🎣 Выберите рыбу:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def handle_product_selection(update, context):
    query = update.callback_query
    query.answer()

    product_id = int(query.data)
    products = context.user_data.get('products', [])
    product = next((p for p in products if p['id'] == product_id), None)

    if not product:
        query.edit_message_text('❌ Товар не найден')
        return

    message = (
        f"🐟 <b>{product['title']}</b>\n\n"
        f"{product['description']}\n\n"
        f"💵 Цена: {product['price']} руб."
    )
    query.edit_message_text(text=message, parse_mode='HTML')


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

        dispatcher.add_handler(CommandHandler('start', start))
        dispatcher.add_handler(CallbackQueryHandler(handle_product_selection))

        logger.info('Бот запущен')
        updater.start_polling()
        updater.idle()

    except redis.exceptions.ConnectionError as e:
        logger.critical(f'Ошибка подключения к Redis: {e}')
    except requests.exceptions.RequestException as e:
        logger.critical(f'Ошибка при работе с API: {e}')
    except Exception as e:
        logger.critical(f'Фатальная ошибка: {e}', exc_info=True)


if __name__ == '__main__':
    main()
