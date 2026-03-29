import os
import json
from asgiref.sync import sync_to_async
from openai import AsyncOpenAI
from dotenv import load_dotenv

from django.core.management.base import BaseCommand
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes
)

from chatbot.models import BotSetting, Prospect

load_dotenv()

DEFAULT_SYSTEM_PROMPT = """
You are a proactive, empathetic, and persuasive virtual assistant for Paradise Tour Travel Agency. 

CRITICAL LANGUAGE RULE: You MUST always respond in the exact language the user is speaking. Once a language is detected, never mix languages.

Your goal is to confidently collect prospect information across TWO STRICT PHASES.
GOLDEN RULES FOR DATA COLLECTION:
- PHASE 1 (Client Info): You must FIRST collect Name, Phone, Email, and ask for a photo of their ID (INE or passport).
- PHASE 2 (Booking Specs): ONLY AFTER you have successfully collected ALL Phase 1 data, you will ask for their booking preferences: Dates, Total nights, and Passengers (explicitly asking how many ADULTS and how many CHILDREN).
DO NOT ask for Phase 2 information if Phase 1 is incomplete. DO NOT ask for everything at once. Keep it conversational but persuasive.

STRICT GUARDRAILS & VALIDATION:
1. ONLY discuss travel/tours: You MUST politely decline to discuss any off-topic subjects outside Paradise Tour Travel Agency.
2. Data Validation: Verify data format. Phones must be digits of reasonable length. Emails must contain '@'. Names must not be obvious fake gibberish (e.g. 'asdf'). Decline fake data gracefully.
3. INE Photo Enforcement: The user MUST upload an actual photo file. Do NOT accept text claims like "here is my ID". You can only proceed if the system explicitly tells you "[El usuario envió una foto de su ID/INE válida]".
4. Finish Conversation: You MUST call the `mark_conversation_finished` tool as soon as the booking flow is complete to shut off automated reminders.

BUSINESS RULES:
- Anticipation Payment Notification: When the conversation is ending, you MUST explicitly inform the customer that an advance payment (anticipo) will be required to confirm the reservation, and a human advisor will detail it shortly.
- Office Hours & Closure: Mon-Fri 10am-2pm and 4pm-6pm, Sat 10am-1pm. Sundays closed. Calculate mentally if the current time matches human hours. If outside these hours, you MUST inform them that their data was received securely and an advisor will contact them in the next available operating block.

Use modern emojis to maintain a professional yet warm interaction (e.g., 🌊, 🏨, ✅). Always highlight the trust, safety, and exclusivity of our agency.
"""

tools = [
    {
        "type": "function",
        "function": {
            "name": "save_prospect_info",
            "description": "Call this whenever the user provides ANY piece of contact or travel details (name, phone, dates, etc.) to save it into the database for the human advisor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {"type": "string", "description": "Language used by user (es/en)"},
                    "name": {"type": "string"},
                    "phone": {"type": "string", "description": "Format +1234567890 digits only"},
                    "email": {"type": "string"},
                    "dates": {"type": "string"},
                    "nights": {"type": "string"},
                    "passengers_adult": {"type": "integer"},
                    "passengers_child": {"type": "integer"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_conversation_finished",
            "description": "CRITICAL: You MUST call this function IMMEDIATELY in the same response where you confirm the booking data is fully registered and you inform the user about the advance payment. If you don't call this, the user will be annoyed by bugged reminders.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

async def call_save_prospect(telegram_id, args):
    try:
        prospect, created = await sync_to_async(Prospect.objects.get_or_create)(telegram_id=telegram_id)
        
        if 'language' in args: prospect.language = args['language']
        if 'name' in args: prospect.name = args['name']
        if 'phone' in args: prospect.phone = str(args['phone']).replace(" ", "")
        if 'email' in args: prospect.email = args['email']
        if 'dates' in args: prospect.dates = args['dates']
        if 'nights' in args: prospect.nights = str(args['nights'])
        if 'passengers_adult' in args: prospect.passengers_adult = args['passengers_adult']
        if 'passengers_child' in args: prospect.passengers_child = args['passengers_child']
        
        await sync_to_async(prospect.full_clean)()
        await sync_to_async(prospect.save)()
        
        is_complete = bool(prospect.name and prospect.phone and prospect.email and prospect.ine_file_id and prospect.dates and prospect.nights and prospect.passengers_adult is not None)
        if is_complete:
            return "Info has been securely saved. The prospect profile is now 100% COMPLETE. YOU MUST NOW CALL mark_conversation_finished IMMEDIATELY to close the flow."
        return "Info has been securely saved in the database."
    except Exception as e:
        return f"Validation error at backend. Tell the user EXACTLY which field was rejected and gracefully ask them for it correctly formatted: {str(e)}"

async def trigger_reengagement(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    user_data = context.job.data or {}
    
    if user_data.get('is_finished', False):
        return
        
    if 'history' not in user_data:
        return
        
    attempts = user_data.get('reengagement_attempts', 0) + 1
    user_data['reengagement_attempts'] = attempts
        
    try:
        setting = await sync_to_async(BotSetting.objects.first)()
        api_key = setting.openai_api_key if setting and setting.openai_api_key else os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            return
            
        client = AsyncOpenAI(api_key=api_key)
        ai_model = setting.ai_model if setting and setting.ai_model else "gpt-3.5-turbo"
        
        if attempts <= 3:
            prompt_text = f"Intento de reconexión {attempts}/3. Han pasado 15 minutos sin respuesta del usuario. Escribe un mensaje CORTO, muy persuasivo y amigable preguntando si aún necesita ayuda o si le quedó alguna duda sobre lo que hablábamos. Mantén el tono pasivo y no presiones."
        else:
            prompt_text = "El usuario no ha respondido en mucho tiempo. Escribe un mensaje de DESPEDIDA MUY CORTO y amigable, informando que dejarás la plática en pausa pero que estarás pendiente y dispuesto a ayudar para futuros mensajes."
            user_data['is_finished'] = True
            
        user_data['history'].append({
            "role": "system",
            "content": prompt_text
        })
        
        response = await client.chat.completions.create(
            model=ai_model,
            messages=user_data['history'],
            temperature=0.7,
            max_tokens=150
        )
        
        bot_reply = response.choices[0].message.content
        user_data['history'].append({"role": "assistant", "content": bot_reply})
        
        await context.bot.send_message(chat_id=chat_id, text=bot_reply, parse_mode='Markdown')
        
        if attempts < 4 and context.job_queue and not user_data.get('is_finished', False):
            context.job_queue.run_once(trigger_reengagement, 900, chat_id=chat_id, name=str(chat_id), data=user_data)
            
    except Exception as e:
        print("Reengagement Error:", e)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    
    # Check job_queue exists
    if context.job_queue:
        current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
        for job in current_jobs:
            job.schedule_removal()
        
    if 'is_finished' not in context.user_data:
        context.user_data['is_finished'] = False
    context.user_data['reengagement_attempts'] = 0

    try:
        setting = await sync_to_async(BotSetting.objects.first)()
    except Exception:
        setting = None
        
    api_key = setting.openai_api_key if setting and setting.openai_api_key else os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        await update.message.reply_text("⚠️ API Key de OpenAI no configurada. Por favor añádela en el .env o tu Panel Global.")
        return

    ai_model = setting.ai_model if setting and setting.ai_model else "gpt-3.5-turbo"
    temperature = setting.temperature if setting and setting.temperature else 0.7
    max_tokens = setting.max_tokens if setting and setting.max_tokens else 500
    base_sys_prompt = setting.system_instructions if setting and setting.system_instructions else DEFAULT_SYSTEM_PROMPT

    import datetime
    current_time = datetime.datetime.now().strftime("CURRENT LOCAL TIME: %A, %H:%M. Use this to determine if we are currently inside or outside human office hours.")
    sys_prompt = f"{base_sys_prompt}\n\n[SYSTEM CLOCK] {current_time}"

    client = AsyncOpenAI(api_key=api_key)

    if 'history' not in context.user_data:
        context.user_data['history'] = [{"role": "system", "content": sys_prompt}]
        
        text_lower = (update.message.text or "").strip().lower()
        
        is_english = any(word in text_lower for word in ['hello', 'hi', 'hey', 'how', 'english'])
        if is_english:
            welcome_msg = "Hello! 🌴 Thank you for contacting Paradise Tour Travel Agency. How can we help you today?"
        else:
            welcome_msg = setting.welcome_message if setting and setting.welcome_message else "¡Hola! 🌴 Gracias por comunicarte con Paradise Tour Travel Agency ¿Cómo podemos ayudarle?"
            
        await update.message.reply_text(welcome_msg)
        context.user_data['history'].append({"role": "assistant", "content": welcome_msg})
        
        if text_lower in ['/start', 'hola', 'hi', 'hello', 'buenas', 'buenos dias', 'buenas tardes', 'hey']:
            if context.job_queue:
                context.job_queue.run_once(trigger_reengagement, 900, chat_id=chat_id, name=str(chat_id), data=context.user_data)
            else:
                print("ADVERTENCIA: JobQueue no está activo.")
            return
            
    context.user_data['history'][0] = {"role": "system", "content": sys_prompt}

    user_text = update.message.text or ""
    
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        prospect, _ = await sync_to_async(Prospect.objects.get_or_create)(telegram_id=str(update.message.from_user.id))
        prospect.ine_file_id = file_id
        await sync_to_async(prospect.save)()
        
        try:
            # Obtener el archivo desde los servidores de Telegram
            tg_file = await context.bot.get_file(file_id)
            img_url = tg_file.file_path
            
            # Usamos explícitamente gpt-4o-mini con capacidades visuales para leer el INE y validar
            ocr_response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Verify if this image is an official ID card (INE/Passport) representing a real person. If it IS an official ID card, extract the Full Name, Address, and Date of Birth strictly. If it is NOT an official ID card (e.g. a drawing, a landscape, a random object, a pet), output EXACTLY the phrase 'INVALID_IMAGE_REJECT'."},
                            {
                                "type": "image_url",
                                "image_url": {"url": img_url}
                            }
                        ]
                    }
                ],
                max_tokens=300
            )
            extracted_text = ocr_response.choices[0].message.content.strip()
            caption = update.message.caption or ""
            
            if "INVALID_IMAGE_REJECT" in extracted_text.upper():
                user_text = f"[El usuario adjuntó una foto que la IA visual clasificó como NO válida o ilegible. Pide educadamente que mande una foto real y clara de su identificación oficial.] {caption}"
            else:
                user_text = f"[El usuario envió una foto de su ID/INE válida]. Datos extraídos: {extracted_text}. {caption}"
        except Exception as e:
            print(f"Vision OCR Error: {e}")
            user_text = f"[El usuario envió una foto de su ID/INE] {update.message.caption or ''}"
            
    elif update.message.document:
        user_text = f"[El usuario envió un documento] {update.message.caption or ''}"

    if not user_text.strip():
        user_text = "[Multimedia no compatible]"

    context.user_data['history'].append({"role": "user", "content": user_text})

    try:
        response = await client.chat.completions.create(
            model=ai_model,
            messages=context.user_data['history'],
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice="auto"
        )
        
        response_message = response.choices[0].message
        
        if response_message.tool_calls:
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                
                if function_name == "mark_conversation_finished":
                    context.user_data['is_finished'] = True
                    function_response = "Conversation marked as finished successfully."
                elif function_name == "save_prospect_info":
                    function_args = json.loads(tool_call.function.arguments)
                    function_response = await call_save_prospect(str(update.message.from_user.id), function_args)
                else:
                    function_response = "Unknown function"
                    
                context.user_data['history'].append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": tool_call.type,
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments
                            }
                        }
                    ]
                })
                context.user_data['history'].append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": function_response
                })
            
            second_response = await client.chat.completions.create(
                model=ai_model,
                messages=context.user_data['history']
            )
            bot_reply = second_response.choices[0].message.content
        else:
            bot_reply = response_message.content

        context.user_data['history'].append({"role": "assistant", "content": bot_reply})
        
        if len(context.user_data['history']) > 20:
            context.user_data['history'] = [context.user_data['history'][0]] + context.user_data['history'][-19:]
            
        await update.message.reply_text(bot_reply, parse_mode='Markdown')

        if context.job_queue and not context.user_data.get('is_finished', False):
            # 900 segundos = 15 minutos
            context.job_queue.run_once(trigger_reengagement, 900, chat_id=chat_id, name=str(chat_id), data=context.user_data)
        elif context.job_queue is None:
            print("ADVERTENCIA: JobQueue no está activo.")

    except Exception as e:
        print(f"OpenAI Error: {e}")
        await update.message.reply_text("Lo siento, estoy teniendo un problema de red en este momento.")

class Command(BaseCommand):
    help = 'Run the Telegram Bot'

    def handle(self, *args, **options):
        token = os.getenv("TELEGRAM_TOKEN")
        self.stdout.write(self.style.SUCCESS("Iniciando Bot Paradise Tour (GPT-3.5 Auto-Reenganche)..."))
        
        app = ApplicationBuilder().token(token).build()

        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
        
        self.stdout.write(self.style.SUCCESS("Bot escuchando a Telegram. Pulsa Ctrl+C para detener..."))
        app.run_polling()
