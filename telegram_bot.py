import os
import json
import logging
import time
from io import BytesIO
from typing import Optional, Dict, List, Any, Union
from datetime import datetime
import re

import redis
import requests
from environs import Env
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    filters,
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    Filters,
)

logger = logging.getLogger(__name__)

# Define all states at the beginning of the file
START, HANDLE_MENU, HANDLE_PRODUCTS, HANDLE_CART, HANDLE_QUANTITY, WAITING_EMAIL = range(6)


def get_database_connection(env):
    """Get Redis connection"""
    return redis.Redis(
        host=env.str('REDIS_HOST'),
        port=env.int('REDIS_PORT'),
        password=env.str('REDIS_PASSWORD'),
        decode_responses=True
    )


def get_products(env):
    """Get all products from Strapi"""
    strapi = StrapiClient(env)
    return strapi.get_products()


def start(update, context):
    """Initial command - shows welcome message and main menu"""
    try:
        strapi = context.bot_data['strapi']
        products = strapi.get_products()
        context.user_data['products'] = products

        # Welcome message
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
            query = update.callback_query
            safe_edit_message(
                query, context, welcome_text, keyboard, parse_mode='HTML'
            )

        return HANDLE_MENU
    except StrapiError as e:
        logger.error(f"Strapi error in start: {e}")
        error_message = '⚠️ Не удалось загрузить данные. Попробуйте позже.'
        if update.message:
            update.message.reply_text(error_message)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Unexpected error in start: {e}")
        error_message = '⚠️ Произошла ошибка. Попробуйте позже.'
        if update.message:
            update.message.reply_text(error_message)
        return ConversationHandler.END


def show_products_list(update, context):
    """Показывает список всех продуктов"""
    query = update.callback_query
    query.answer()

    strapi = context.bot_data['strapi']
    products = strapi.get_products()
    context.user_data['products'] = products

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
    """Add product to cart"""
    try:
        product_id = int(product_id_str)
        strapi = context.bot_data['strapi']

        if update.callback_query:
            query = update.callback_query
            tg_id = str(query.from_user.id)
            chat_id = query.message.chat_id
        else:
            tg_id = str(update.message.from_user.id)
            chat_id = update.message.chat_id

        # Get product info
        product = get_selected_product(str(product_id), context)
        if not product:
            raise StrapiError("Product not found")

        # Get or create cart
        cart = strapi.get_cart(tg_id)
        if not cart:
            cart = strapi.create_cart(tg_id)

        # Add to cart
        strapi.add_to_cart(cart['id'], product_id, quantity)

        # Success message
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

    except StrapiError as e:
        logger.error(f"Strapi error in add_to_cart_handler: {e}")
        error_text = f'❌ Не удалось добавить товар в корзину: {str(e)}'
        if update.callback_query:
            update.callback_query.answer(error_text)
            return show_products_list(update, context)
        else:
            update.message.reply_text(error_text)
            return start(update, context)
    except Exception as e:
        logger.error(f"Unexpected error in add_to_cart_handler: {e}")
        error_text = '❌ Произошла ошибка при добавлении в корзину'
        if update.callback_query:
            update.callback_query.answer(error_text)
            return show_products_list(update, context)
        else:
            update.message.reply_text(error_text)
            return start(update, context)


def view_cart(update, context):
    """Show cart contents with grouped identical products"""
    query = update.callback_query
    if query:
        query.answer()

    try:
        tg_id = str(query.from_user.id if query else update.message.from_user.id)
        logger.info(f"Viewing cart for user {tg_id}")

        strapi = context.bot_data['strapi']
        cart = strapi.get_cart(tg_id)

        if not cart:
            logger.info(f"No cart found for user {tg_id}")
            empty_cart_text = (
                "🛒 <b>Ваша корзина пуста</b>\n\n"
                "Добавьте товары из каталога!"
            )
            keyboard = [
                [InlineKeyboardButton('🎣 Посмотреть рыбу', callback_data='show_products')],
                [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
            ]

            if query:
                safe_edit_message(query, context, empty_cart_text, keyboard, parse_mode='HTML')
            else:
                update.message.reply_text(
                    empty_cart_text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return HANDLE_MENU

        # Log cart contents with full data
        cart_items = cart.get('cart_products', [])
        logger.info("Cart contents:")
        for item in cart_items:
            logger.info(f"Cart item: id={item.get('id')}, documentId={item.get('documentId')}, "
                        f"product_id={item.get('product', {}).get('id')}, "
                        f"quantity={item.get('quantity')}, "
                        f"full_data={json.dumps(item, indent=2)}")

        # Group identical products
        grouped_items = {}
        for item in cart_items:
            if not item.get('product'):
                logger.warning(f"Cart item without product: {item}")
                continue

            product = item['product']
            product_id = product['documentId']  # Используем documentId для группировки
            
            if product_id not in grouped_items:
                grouped_items[product_id] = {
                    'product': product,
                    'total_quantity': 0,
                    'total_price': 0,
                    'cart_items': []  # Сохраняем все cart_items для возможности удаления
                }
            
            grouped_items[product_id]['cart_items'].append(item)
            grouped_items[product_id]['total_quantity'] += item['quantity']
            grouped_items[product_id]['total_price'] += item['quantity'] * product['price']

        # Format cart message
        message = "🛒 <b>Ваша корзина:</b>\n\n"
        total = 0
        keyboard = []

        # Add grouped items to message and keyboard
        for product_id, group in grouped_items.items():
            product = group['product']
            total_quantity = group['total_quantity']
            item_total = group['total_price']
            total += item_total

            # Форматируем количество с учетом десятичных знаков
            quantity_str = f"{total_quantity:.1f}".rstrip('0').rstrip('.')
            
            message += (
                f"🐟 <b>{product['title']}</b>\n"
                f"📦 {quantity_str} кг × {product['price']} руб. = {item_total} руб.\n\n"
            )

            # Добавляем кнопку удаления для товара
            # Используем первый cart_item для удаления
            first_cart_item = group['cart_items'][0]
            cart_item_document_id = first_cart_item['documentId']
            
            keyboard.append([InlineKeyboardButton(
                f"❌ Удалить {product['title']}",
                callback_data=f'remove_{cart_item_document_id}'
            )])

        message += f"💵 <b>Итого: {total} руб.</b>"

        # Add main buttons
        keyboard.extend([
            [InlineKeyboardButton('📞 Оформить заказ', callback_data='checkout')],
            [InlineKeyboardButton('🎣 Продолжить покупки', callback_data='show_products')],
            [InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]
        ])

        # Send message
        if query:
            try:
                # Always delete old message to ensure fresh view
                context.bot.delete_message(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id
                )
            except Exception as e:
                logger.warning(f"Failed to delete old message: {e}")

            # Send new message
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text=message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
        else:
            update.message.reply_text(
                message,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        return HANDLE_CART

    except StrapiError as e:
        logger.error(f"Strapi error in view_cart: {e}")
        error_text = f'⚠️ Не удалось загрузить корзину: {str(e)}'
        keyboard = [[InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]]

        if query:
            safe_edit_message(query, context, error_text, keyboard)
        else:
            update.message.reply_text(error_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return HANDLE_MENU
    except Exception as e:
        logger.error(f"Unexpected error in view_cart: {e}")
        error_text = '⚠️ Произошла ошибка при загрузке корзины'
        keyboard = [[InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')]]

        if query:
            safe_edit_message(query, context, error_text, keyboard)
        else:
            update.message.reply_text(error_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return HANDLE_MENU


def handle_cart_actions(update, context):
    """Handle cart actions"""
    query = update.callback_query
    query.answer()

    if query.data == 'main_menu':
        return start(update, context)
    elif query.data == 'show_products':
        return show_products_list(update, context)
    elif query.data == 'checkout':
        return handle_checkout(update, context)
    elif query.data.startswith('remove_'):
        try:
            item_id = query.data.split('_')[1]
            logger.info(f"Processing cart item removal. Item ID: {item_id} (type: {type(item_id)})")
            return remove_from_cart(update, context, item_id)
        except Exception as e:
            logger.error(f"Error processing remove action: {e}")
            logger.error("Full error details:", exc_info=True)
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text='❌ Ошибка при обработке удаления'
            )
            return HANDLE_CART

    return HANDLE_CART


def checkout(update, context):
    """Handle checkout process"""
    query = update.callback_query
    query.answer()
    
    user_id = str(update.effective_user.id)
    strapi = context.bot_data['strapi']
    
    # Get user's cart
    cart = strapi.get_cart(user_id)
    logger.info(f"Cart data for user {user_id}: {cart}")
    
    # Проверяем структуру корзины
    if not cart:
        logger.warning(f"Cart is None for user {user_id}")
        query.edit_message_text(
            "Ваша корзина пуста. Добавьте товары перед оформлением заказа.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Вернуться в меню", callback_data="main_menu")
            ]])
        )
        return HANDLE_MENU
    
    # Получаем товары корзины так же, как в view_cart
    cart_items = cart.get('cart_products', [])
    logger.info(f"Cart items: {cart_items}")
    
    if not cart_items:
        logger.warning(f"No products in cart for user {user_id}")
        query.edit_message_text(
            "Ваша корзина пуста. Добавьте товары перед оформлением заказа.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Вернуться в меню", callback_data="main_menu")
            ]])
        )
        return HANDLE_MENU
    
    # Request email from user
    query.edit_message_text(
        "Для оформления заказа, пожалуйста, введите ваш email:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Отмена", callback_data="main_menu")
        ]])
    )
    return WAITING_EMAIL


def create_order(update, context):
    """Create order after email is collected"""
    user_id = str(update.effective_user.id)
    strapi = context.bot_data['strapi']
    
    # Get user's cart
    cart = strapi.get_cart(user_id)
    logger.info(f"Cart data in create_order for user {user_id}: {cart}")
    
    if not cart:
        logger.warning(f"Cart is None in create_order for user {user_id}")
        update.message.reply_text(
            "Ваша корзина пуста. Добавьте товары перед оформлением заказа.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Вернуться в меню", callback_data="main_menu")
            ]])
        )
        return HANDLE_MENU
    
    # Получаем товары корзины так же, как в view_cart
    cart_items = cart.get('cart_products', [])
    logger.info(f"Cart items in create_order: {cart_items}")
    
    if not cart_items:
        logger.warning(f"No products in cart in create_order for user {user_id}")
        update.message.reply_text(
            "Ваша корзина пуста. Добавьте товары перед оформлением заказа.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Вернуться в меню", callback_data="main_menu")
            ]])
        )
        return HANDLE_MENU
    
    # Create order
    try:
        # Получаем email из сообщения пользователя
        email = update.message.text.strip()
        
        # Создаем заказ с email
        order = strapi.create_order(user_id, email)
        logger.info(f"Order creation result: {order}")
        
        if order:
            # Clear cart after successful order creation
            strapi.clear_cart(user_id)
            
            update.message.reply_text(
                "Спасибо за заказ! Мы свяжемся с вами в ближайшее время для уточнения деталей.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Вернуться в меню", callback_data="main_menu")
                ]])
            )
        else:
            update.message.reply_text(
                "Произошла ошибка при создании заказа. Пожалуйста, попробуйте позже.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Вернуться в меню", callback_data="main_menu")
                ]])
            )
    except Exception as e:
        logger.error(f"Error creating order: {e}")
        update.message.reply_text(
            "Произошла ошибка при создании заказа. Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Вернуться в меню", callback_data="main_menu")
            ]])
        )
    
    return HANDLE_MENU


def handle_email(update, context):
    """Handle email input and create order"""
    try:
        email = update.message.text.strip()
        
        # Basic email validation
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            update.message.reply_text(
                "⚠️ Пожалуйста, введите корректный email адрес.\n"
                "Например: user@example.com",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('🏠 Отмена', callback_data='main_menu')
                ]])
            )
            return WAITING_EMAIL

        # Proceed to order creation
        return create_order(update, context)

    except Exception as e:
        logger.error(f"Unexpected error in handle_email: {e}")
        update.message.reply_text(
            "⚠️ Произошла ошибка при сохранении данных.\n"
            "Пожалуйста, попробуйте позже или обратитесь в поддержку.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('🏠 В главное меню', callback_data='main_menu')
            ]])
        )
        return HANDLE_MENU


def remove_from_cart(update, context, item_id):
    """Remove item from cart with enhanced verification"""
    query = update.callback_query
    query.answer('⏳ Удаляем товар...')

    try:
        logger.info(f"Starting remove_from_cart for item {item_id}")
        
        # Get cart and verify item exists
        strapi = context.bot_data['strapi']
        cart = strapi.get_cart(str(query.from_user.id))
        if not cart:
            logger.error("Cart not found")
            raise StrapiError("Cart not found")

        cart_id = cart['id']
        cart_items = cart.get('cart_products', [])
        
        # Verify item exists in cart using documentId
        item_exists = any(str(item.get('documentId')) == str(item_id) for item in cart_items)
        if not item_exists:
            logger.error(f"Item {item_id} not found in cart")
            raise StrapiError("Item not found in cart")

        # Try to remove with retries
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Removal attempt {attempt + 1}/{max_retries}")
                
                # Delete the item using documentId
                strapi.remove_from_cart(str(item_id), str(query.from_user.id))
                
                # Wait for changes to propagate
                time.sleep(retry_delay)
                
                # Verify removal using documentId
                cart_after = strapi.get_cart(str(query.from_user.id))
                if not cart_after:
                    logger.error("Could not get cart after deletion")
                    continue
                    
                cart_items_after = cart_after.get('cart_products', [])
                item_still_exists = any(str(item.get('documentId')) == str(item_id) for item in cart_items_after)
                
                if not item_still_exists:
                    logger.info("Item removal verified")
                    return view_cart(update, context)
                
                logger.warning(f"Item still exists after attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                
            except StrapiError as e:
                logger.error(f"Error during removal attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                raise

        raise StrapiError("Could not remove item after all attempts")

    except StrapiError as e:
        logger.error(f"Error in remove_from_cart: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in remove_from_cart: {e}")
        logger.error("Full error details:", exc_info=True)
        raise StrapiError(f"Failed to remove item: {str(e)}")


def get_selected_product(product_id_str: str, context) -> Optional[Dict]:
    """Get product by ID from cached products"""
    try:
        product_id = int(product_id_str)
    except ValueError:
        return None

    products = context.user_data.get('products', [])
    return next((p for p in products if p['id'] == product_id), None)


def show_product_details(query, context, product):
    """Show detailed product information"""
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

    # Check if product has image
    if product.get('image') and product['image'].get('url'):
        try:
            # Delete current message
            context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
            # Send photo with description
            send_product_photo(query, context, product, message_text, InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Error sending photo: {e}")
            # Fallback to text if photo fails
            safe_edit_message(query, context, message_text, keyboard, parse_mode='HTML')
    else:
        safe_edit_message(query, context, message_text, keyboard, parse_mode='HTML')


def safe_edit_message(query, context, text, keyboard, parse_mode=None):
    """Safely edit message with fallback to new message"""
    try:
        # Check if content is different
        current_text = query.message.text or query.message.caption or ""
        if current_text.strip() == text.strip():
            return

        query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.warning(f"Failed to edit message: {e}")
        try:
            # Delete old message
            context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
        except Exception:
            pass

        # Send new message
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


def send_product_photo(query, context, product, caption, reply_markup):
    """Send product photo"""
    try:
        base_url = context.bot_data['env'].str('STRAPI_URL').rstrip('/')
        image_path = product['image']['url']

        # Check if path is full URL or relative
        if image_path.startswith('http'):
            image_url = image_path
        else:
            image_path = image_path.lstrip('/')
            image_url = f"{base_url}/{image_path}"

        logger.info(f"Loading image: {image_url}")

        response = requests.get(image_url, stream=True, timeout=10)
        response.raise_for_status()

        # Verify it's an image
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            logger.error(f"Invalid content type: {content_type}")
            raise ValueError("File is not an image")

        with BytesIO(response.content) as photo_data:
            photo_data.seek(0)
            context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo_data,
                caption=caption,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            logger.info("Image sent successfully")

    except requests.exceptions.Timeout:
        logger.error("Timeout loading image")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error loading image: {e}")
        raise
    except Exception as e:
        logger.error(f"Error sending photo: {e}")
        raise


def error_handler(update, context):
    """Handle errors"""
    logger.error("Error during update:", exc_info=context.error)

    if update and update.effective_chat:
        try:
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text='⚠️ Произошла ошибка. Попробуйте еще раз или начните заново с команды /start'
            )
        except Exception:
            pass


class StrapiError(Exception):
    """Base exception for Strapi API errors"""
    pass


class StrapiClient:
    """Client for interacting with Strapi API"""

    def __init__(self, api_url, token):
        # Ensure API URL ends with /api
        self.api_url = api_url.rstrip('/')
        if not self.api_url.endswith('/api'):
            self.api_url = f"{self.api_url}/api"
            
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        })
        self._api_endpoints = {
            'products': '/products',
            'carts': '/carts',
            'cart-products': '/cart-products',
            'orders': '/orders'  # Это будет работать после создания коллекции в Strapi
        }

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Make request to Strapi API"""
        try:
            # Use the endpoint directly from our predefined list
            url = f"{self.api_url}{self._api_endpoints.get(endpoint, f'/{endpoint}')}"
            
            # Add cache busting for GET requests
            if method.upper() == 'GET':
                params = kwargs.get('params', {})
                params['_t'] = int(time.time())
                kwargs['params'] = params

            response = self.session.request(method, url, **kwargs)
            
            # For DELETE requests, consider both 200 and 204 as success
            if method.upper() == 'DELETE':
                if response.status_code in (200, 204):
                    logger.info(f"DELETE request successful (status {response.status_code})")
                    return {'success': True}
                response.raise_for_status()
            
            # For other methods, raise for any non-2xx status
            if not 200 <= response.status_code < 300:
                response.raise_for_status()
            
            # Try to parse JSON only if there's content
            if response.content:
                try:
                    return response.json()
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON response: {e}")
                    logger.error(f"Response content: {response.text}")
                    raise
            else:
                logger.info("Response has no content, returning success")
                return {'success': True}
                
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response text: {e.response.text}")
            raise StrapiError(f"API request failed: {str(e)}")

    def get_products(self) -> List[Dict]:
        """Get all products with their details"""
        try:
            params = {
                'populate': '*',
                '_t': str(int(time.time() * 1000))
            }
            response = self._make_request('GET', 'products', params=params)
            return response.get('data', [])
        except StrapiError as e:
            logger.error(f"Error fetching products: {e}")
            raise

    def create_cart(self, tg_id: str) -> Dict:
        """Create a new cart for user"""
        try:
            data = {'data': {'tg_id': str(tg_id)}}
            response = self._make_request('POST', 'carts', json=data)
            return response['data']
        except StrapiError as e:
            logger.error(f"Error creating cart: {e}")
            raise

    def add_to_cart(self, cart_id: int, product_id: int, quantity: float) -> Dict:
        """Add product to cart"""
        try:
            # Try different possible endpoint names
            possible_endpoints = [
                'cart-products',  # kebab-case plural
                'cart-product',   # kebab-case singular
                'cart_products',  # snake_case plural
                'cart_product'    # snake_case singular
            ]

            data = {
                "data": {
                    "quantity": float(quantity),
                    "product": int(product_id),
                    "cart": int(cart_id)
                }
            }

            for endpoint in possible_endpoints:
                try:
                    logger.info(f"Trying endpoint: {endpoint}")
                    response = self._make_request('POST', endpoint, json=data)
                    logger.info(f"Found endpoint: {endpoint}")
                    return response['data']
                except StrapiError as e:
                    logger.info(f"Endpoint {endpoint} failed: {e}")
                    continue

            raise StrapiError("Could not find valid endpoint for cart items")

        except StrapiError as e:
            logger.error(f"Error in add_to_cart: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in add_to_cart: {e}")
            raise StrapiError(f"Failed to add item: {str(e)}")

    def verify_removal(self, cart_id: str, item_id: str) -> bool:
        """Verify item removal with direct query using documentId"""
        try:
            # Прямой запрос к cart-products с фильтром по documentId
            params = {
                'filters[documentId][$eq]': item_id,
                'filters[cart][id][$eq]': cart_id,
                '_t': str(int(time.time() * 1000))
            }
            response = self._make_request('GET', 'cart-products', params=params)
            return len(response.get('data', [])) == 0
        except Exception as e:
            logger.error(f"Verification error: {e}")
            return False

    def force_cart_refresh(self, cart_id: str) -> None:
        """Force cart refresh by updating a dummy field"""
        try:
            # Вместо обновления updatedAt, обновляем tg_id на то же значение
            # Это заставит Strapi обновить запись без изменения системных полей
            self._make_request('PUT', f'carts/{cart_id}', 
                             json={'data': {'tg_id': str(cart_id)}})
        except Exception as e:
            logger.error(f"Cart refresh error: {e}")

    def get_cart(self, tg_id: str) -> Optional[Dict]:
        """Get user's cart with fresh data"""
        try:
            params = {
                'filters[tg_id][$eq]': tg_id,
                'populate[cart_products][populate][product]': 'true',
                '_t': str(int(time.time() * 1000))
            }
            response = self._make_request('GET', 'carts', params=params)
            return response.get('data', [None])[0]
        except StrapiError as e:
            logger.error(f"Error fetching cart: {e}")
            raise

    def remove_from_cart(self, item_id: str, tg_id: str) -> bool:
        """Remove item from cart with enhanced verification"""
        try:
            logger.info(f"Starting remove_from_cart for item {item_id}")
            
            # Get cart and verify item exists
            cart = self.get_cart(tg_id)
            if not cart:
                logger.error("Cart not found")
                raise StrapiError("Cart not found")

            cart_id = cart['id']
            cart_items = cart.get('cart_products', [])
            
            # Verify item exists in cart using documentId
            item_exists = any(str(item.get('documentId')) == str(item_id) for item in cart_items)
            if not item_exists:
                logger.error(f"Item {item_id} not found in cart")
                raise StrapiError("Item not found in cart")

            # Try to remove with retries
            max_retries = 3
            retry_delay = 2
            
            for attempt in range(max_retries):
                try:
                    logger.info(f"Removal attempt {attempt + 1}/{max_retries}")
                    
                    # Delete the item using documentId
                    self._make_request('DELETE', f'cart-products/{item_id}')
                    
                    # Force cart refresh
                    self.force_cart_refresh(cart_id)
                    
                    # Wait for changes to propagate
                    time.sleep(retry_delay)
                    
                    # Verify removal using documentId
                    if self.verify_removal(cart_id, item_id):
                        logger.info("Item removal verified")
                        return True
                    
                    logger.warning(f"Item still exists after attempt {attempt + 1}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    
                except StrapiError as e:
                    logger.error(f"Error during removal attempt {attempt + 1}: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    raise

            raise StrapiError("Could not remove item after all attempts")

        except StrapiError as e:
            logger.error(f"Error in remove_from_cart: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in remove_from_cart: {e}")
            logger.error("Full error details:", exc_info=True)
            raise StrapiError(f"Failed to remove item: {str(e)}")

    def get_client(self, tg_id):
        """Get client by telegram ID"""
        try:
            # Используем _make_request вместо прямого вызова session.get
            response = self._make_request(
                'GET',
                'clients',
                params={
                    'filters[tg_id][$eq]': tg_id,
                    '_t': int(time.time())  # Cache busting
                }
            )
            
            if response.get('data') and len(response['data']) > 0:
                return response['data'][0]
            return None
        except Exception as e:
            logger.error(f"Error getting client: {e}")
            raise StrapiError(f"Failed to get client: {str(e)}")

    def create_client(self, tg_id, email, username=None):
        """Create new client or update existing one"""
        try:
            # Check if client already exists
            existing_client = self.get_client(tg_id)
            if existing_client:
                # Update existing client
                client_id = existing_client['id']
                response = self._make_request(
                    'PUT',
                    f'clients/{client_id}',
                    json={
                        'data': {
                            'email': email,
                            'username': username,
                            'tg_id': tg_id
                        }
                    }
                )
            else:
                # Create new client
                response = self._make_request(
                    'POST',
                    'clients',
                    json={
                        'data': {
                            'email': email,
                            'username': username,
                            'tg_id': tg_id
                        }
                    }
                )
            
            return response['data']
        except Exception as e:
            logger.error(f"Error creating/updating client: {e}")
            raise StrapiError(f"Failed to create/update client: {str(e)}")

    def create_order(self, tg_id: str, email: str) -> Dict:
        """Create a new order"""
        try:
            # Get cart data
            cart = self.get_cart(tg_id)
            if not cart:
                raise StrapiError("Cart not found")

            cart_items = cart.get('cart_products', [])
            if not cart_items:
                raise StrapiError("Cart is empty")

            # Calculate total and prepare cart product IDs
            total = 0
            cart_product_ids = []
            for item in cart_items:
                total += item['quantity'] * item['product']['price']
                cart_product_ids.append(item['id'])

            # Prepare order data
            order_data = {
                'data': {
                    'email': email,
                    'order_status': 'new',  # Используем правильное название поля
                    'total': total,
                    'cart_products': {
                        'connect': cart_product_ids
                    }
                }
            }

            logger.info(f"Creating order with data: {json.dumps(order_data, indent=2)}")

            # Create order
            try:
                response = self._make_request('POST', 'orders', json=order_data)
                if response and response.get('data'):
                    logger.info(f"Order created successfully: {response}")
                    # Clear cart after successful order creation
                    self.clear_cart(tg_id)
                    return response['data']
            except StrapiError as e:
                if "400" in str(e):
                    error_msg = str(e)
                    if "order_status" in error_msg.lower():
                        raise StrapiError(
                            "Could not create order. Please check that:\n"
                            "1. The 'order_status' field in Order collection is set up as an enumeration\n"
                            "2. The enumeration values are exactly: new, processing, completed, cancelled\n"
                            "3. The values are case-sensitive"
                        )
                    elif "cart_products" in error_msg.lower():
                        raise StrapiError(
                            "Could not create order. Please check that:\n"
                            "1. The 'cart_products' field in Order collection is set up as a relation to CartProduct\n"
                            "2. The relation is configured as 'one-to-many' or 'many-to-many'\n"
                            "3. The Public role has permissions to create relations"
                        )
                elif "404" in str(e):
                    raise StrapiError(
                        "Could not create order. Please make sure that:\n"
                        "1. The 'Order' collection is created in Strapi\n"
                        "2. The collection has the following fields:\n"
                        "   - email (Text)\n"
                        "   - order_status (Enumeration with values: new, processing, completed, cancelled)\n"
                        "   - cart_products (Relation to CartProduct)\n"
                        "   - total (Number)\n"
                        "3. The Public role has permissions to create orders"
                    )
                raise

            raise StrapiError("Failed to create order")

        except Exception as e:
            logger.error(f"Error creating order: {e}")
            raise StrapiError(f"Failed to create order: {str(e)}")

    def clear_cart(self, tg_id: str) -> bool:
        """Clear all items from cart"""
        try:
            cart = self.get_cart(tg_id)
            if not cart:
                return True  # Cart already empty

            cart_items = cart.get('cart_products', [])
            for item in cart_items:
                try:
                    self._make_request('DELETE', f'cart-products/{item["id"]}')
                except Exception as e:
                    logger.error(f"Error removing item {item['id']}: {e}")
                    continue

            return True
        except Exception as e:
            logger.error(f"Error clearing cart: {e}")
            raise StrapiError(f"Failed to clear cart: {str(e)}")


def main():
    """Start the bot"""
    env = Env()
    env.read_env()

    # Initialize bot
    updater = Updater(env.str('TELEGRAM_TOKEN'))
    dispatcher = updater.dispatcher

    # Initialize Strapi client
    strapi = StrapiClient(env.str('STRAPI_URL'), env.str('STRAPI_TOKEN'))
    dispatcher.bot_data['strapi'] = strapi
    dispatcher.bot_data['env'] = env

    # Add conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            HANDLE_MENU: [
                CallbackQueryHandler(show_products_list, pattern='^show_products$'),
                CallbackQueryHandler(view_cart, pattern='^view_cart$'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            HANDLE_PRODUCTS: [
                CallbackQueryHandler(add_to_cart_handler, pattern='^add_\d+$'),
                CallbackQueryHandler(view_cart, pattern='^view_cart$'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            HANDLE_CART: [
                CallbackQueryHandler(remove_from_cart, pattern='^remove_'),
                CallbackQueryHandler(checkout, pattern='^checkout$'),
                CallbackQueryHandler(show_products_list, pattern='^show_products$'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            WAITING_EMAIL: [
                MessageHandler(Filters.regex(r'^[^/].*$'), handle_email),  # Match any text that doesn't start with /
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
        },
        fallbacks=[CommandHandler('start', start)],
        name='fish_store_conversation',
        persistent=False
    )

    dispatcher.add_handler(conv_handler)

    # Start the bot
    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()
