from flask import Flask, request, jsonify
from flask_cors import CORS
import os
from datetime import datetime
from dotenv import load_dotenv
import anthropic
from pymongo import MongoClient
from bson import ObjectId
import uuid

# Cargar variables de entorno
load_dotenv()

app = Flask(__name__)
CORS(app)

# Configurar Claude
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
claude_client = None

if ANTHROPIC_API_KEY:
    try:
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        print("Claude configurado correctamente")
    except Exception as e:
        print(f"Error configurando Claude: {e}")
        claude_client = None
else:
    print("ANTHROPIC_API_KEY no encontrada")

# Configurar MongoDB
MONGODB_URI = os.getenv('MONGODB_URI')
db = None
conversaciones_collection = None
mensajes_collection = None

if MONGODB_URI:
    try:
        client = MongoClient(MONGODB_URI)
        db = client['claude_chat']
        conversaciones_collection = db['conversaciones']
        mensajes_collection = db['mensajes']

        # Probar conexión
        client.admin.command('ping')
        print("MongoDB conectado correctamente")

        # Crear índices para mejor rendimiento
        conversaciones_collection.create_index([("uid", 1), ("updated_at", -1)])
        mensajes_collection.create_index([("conversation_id", 1), ("timestamp", 1)])

    except Exception as e:
        print(f"Error conectando a MongoDB: {e}")
        db = None
else:
    print("MONGODB_URI no encontrada")


def serializar_documento(doc):
    """Convertir ObjectId a string para JSON"""
    if isinstance(doc, dict) and '_id' in doc:
        doc['_id'] = str(doc['_id'])
    return doc


def crear_conversacion(uid):
    """Crear una nueva conversación madre para el usuario"""
    if conversaciones_collection is None:
        raise Exception("MongoDB no configurado")

    conversation_id = str(uuid.uuid4())
    conversacion = {
        'conversation_id': conversation_id,
        'uid': uid,
        'created_at': datetime.now(),
        'updated_at': datetime.now(),
        'message_count': 0,
        'title': 'Nueva conversación',
        'status': 'active'
    }

    result = conversaciones_collection.insert_one(conversacion)
    print(f"Nueva conversación creada: {conversation_id} para usuario: {uid}")
    return conversation_id


def agregar_mensaje(conversation_id, mensaje_usuario, respuesta_claude, uid):
    """Agregar mensajes a la conversación en MongoDB"""
    if mensajes_collection is None or conversaciones_collection is None:
        raise Exception("MongoDB no configurado")

    timestamp = datetime.now()

    # Crear mensajes
    mensajes = [
        {
            'id': str(uuid.uuid4()),
            'conversation_id': conversation_id,
            'uid': uid,
            'role': 'user',
            'content': mensaje_usuario,
            'timestamp': timestamp
        },
        {
            'id': str(uuid.uuid4()),
            'conversation_id': conversation_id,
            'uid': uid,
            'role': 'assistant',
            'content': respuesta_claude,
            'timestamp': timestamp
        }
    ]

    # Insertar mensajes
    mensajes_collection.insert_many(mensajes)

    # Actualizar conversación
    nuevo_count = mensajes_collection.count_documents({'conversation_id': conversation_id})

    update_data = {
        'updated_at': timestamp,
        'message_count': nuevo_count
    }

    # Si es el primer mensaje, actualizar el título
    if nuevo_count == 2:  # Usuario + respuesta = 2 mensajes
        titulo = mensaje_usuario[:50] + "..." if len(mensaje_usuario) > 50 else mensaje_usuario
        update_data['title'] = titulo

    conversaciones_collection.update_one(
        {'conversation_id': conversation_id},
        {'$set': update_data}
    )


def obtener_historial_conversacion(conversation_id):
    """Obtener historial completo de la conversación para Claude"""
    if mensajes_collection is None:
        return []

    # Obtener mensajes ordenados por timestamp
    mensajes = mensajes_collection.find(
        {'conversation_id': conversation_id}
    ).sort('timestamp', 1)

    # Convertir al formato que espera Claude
    historial = []
    for mensaje in mensajes:
        historial.append({
            'role': mensaje['role'],
            'content': mensaje['content']
        })

    return historial


@app.route('/')
def home():
    # Estadísticas de MongoDB
    stats = {"conversaciones": 0, "mensajes": 0}
    if conversaciones_collection is not None and mensajes_collection is not None:
        try:
            stats["conversaciones"] = conversaciones_collection.count_documents({})
            stats["mensajes"] = mensajes_collection.count_documents({})
        except:
            pass

    return {
        "message": "Claude Chat API con MongoDB",
        "status": "OK",
        "claude_status": "active" if claude_client else "inactive",
        "mongodb_status": "connected" if db is not None else "disconnected",
        "stats": stats,
        "endpoints": ["/chat", "/conversations", "/conversations/new", "/health"]
    }


@app.route('/chat', methods=['POST'])
def chat():
    try:
        # Verificar configuración
        if not claude_client:
            return jsonify({
                'success': False,
                'error': 'Claude no configurado'
            }), 500

        if db is None:
            return jsonify({
                'success': False,
                'error': 'MongoDB no configurado'
            }), 500

        # Obtener datos del request
        data = request.get_json()
        if not data:
            return jsonify({
                'success': False,
                'error': 'Datos JSON requeridos'
            }), 400

        # Validar campos requeridos
        required_fields = ['uid', 'message']
        for field in required_fields:
            if field not in data:
                return jsonify({
                    'success': False,
                    'error': f'Campo "{field}" requerido'
                }), 400

        uid = data['uid']
        mensaje = data['message']
        conversation_id = data.get('conversation_id')  # Opcional

        # Validar mensaje
        if not mensaje.strip():
            return jsonify({
                'success': False,
                'error': 'Mensaje no puede estar vacío'
            }), 400

        print(f"Mensaje de usuario {uid}: {mensaje}")

        # CAMBIO IMPORTANTE: Si no hay conversation_id, SIEMPRE crear nueva conversación
        if not conversation_id:
            print(f"No hay conversation_id, creando nueva conversación para {uid}")
            conversation_id = crear_conversacion(uid)
        else:
            # Verificar que la conversación existe y pertenece al usuario
            conversacion = conversaciones_collection.find_one({'conversation_id': conversation_id})
            if not conversacion:
                return jsonify({
                    'success': False,
                    'error': 'Conversación no encontrada'
                }), 404

            if conversacion['uid'] != uid:
                return jsonify({
                    'success': False,
                    'error': 'No tienes acceso a esta conversación'
                }), 403

        # Obtener historial de la conversación (vacío si es nueva)
        historial = obtener_historial_conversacion(conversation_id)

        # Agregar el nuevo mensaje del usuario al historial
        historial.append({
            'role': 'user',
            'content': mensaje
        })

        print(f"Enviando conversación con {len(historial)} mensajes a Claude")

        # Enviar todo el historial a Claude
        response = claude_client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1000,
            messages=historial
        )

        respuesta = response.content[0].text
        print(f"Claude respondió: {len(respuesta)} caracteres")

        # Guardar la conversación en MongoDB
        agregar_mensaje(conversation_id, mensaje, respuesta, uid)

        # Obtener count actualizado
        nuevo_count = mensajes_collection.count_documents({'conversation_id': conversation_id})

        return jsonify({
            'success': True,
            'conversation_id': conversation_id,
            'message': mensaje,
            'response': respuesta,
            'message_count': nuevo_count,
            'is_new_conversation': not data.get('conversation_id'),  # Indica si es nueva
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/conversations', methods=['GET'])
def get_conversations():
    """Obtener todas las conversaciones de un usuario"""
    if conversaciones_collection is None:
        return jsonify({
            'success': False,
            'error': 'MongoDB no configurado'
        }), 500

    uid = request.args.get('uid')
    if not uid:
        return jsonify({
            'success': False,
            'error': 'Parámetro uid requerido'
        }), 400

    try:
        # Obtener conversaciones del usuario
        conversaciones = list(conversaciones_collection.find(
            {'uid': uid},
            sort=[('updated_at', -1)]
        ))

        # Serializar para JSON
        conversaciones_serializadas = [serializar_documento(conv) for conv in conversaciones]

        return jsonify({
            'success': True,
            'uid': uid,
            'conversations': conversaciones_serializadas,
            'total': len(conversaciones_serializadas)
        })

    except Exception as e:
        print(f"Error obteniendo conversaciones: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/conversations/<conversation_id>', methods=['GET'])
def get_conversation(conversation_id):
    """Obtener una conversación específica con sus mensajes"""
    if conversaciones_collection is None or mensajes_collection is None:
        return jsonify({
            'success': False,
            'error': 'MongoDB no configurado'
        }), 500

    uid = request.args.get('uid')
    if not uid:
        return jsonify({
            'success': False,
            'error': 'Parámetro uid requerido'
        }), 400

    try:
        # Obtener conversación
        conversacion = conversaciones_collection.find_one({'conversation_id': conversation_id})

        if not conversacion:
            return jsonify({
                'success': False,
                'error': 'Conversación no encontrada'
            }), 404

        # Verificar permisos
        if conversacion['uid'] != uid:
            return jsonify({
                'success': False,
                'error': 'No tienes acceso a esta conversación'
            }), 403

        # Obtener mensajes
        mensajes = list(mensajes_collection.find(
            {'conversation_id': conversation_id}
        ).sort('timestamp', 1))

        # Serializar datos
        conversacion_serializada = serializar_documento(conversacion)
        mensajes_serializados = [serializar_documento(msg) for msg in mensajes]

        conversacion_serializada['messages'] = mensajes_serializados

        return jsonify({
            'success': True,
            'conversation': conversacion_serializada
        })

    except Exception as e:
        print(f"Error obteniendo conversación: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/conversations/new', methods=['POST'])
def new_conversation():
    """Crear una nueva conversación para el usuario"""
    if conversaciones_collection is None:
        return jsonify({
            'success': False,
            'error': 'MongoDB no configurado'
        }), 500

    data = request.get_json()
    if not data or 'uid' not in data:
        return jsonify({
            'success': False,
            'error': 'Campo uid requerido'
        }), 400

    try:
        uid = data['uid']
        conversation_id = crear_conversacion(uid)

        # Obtener la conversación creada
        conversacion = conversaciones_collection.find_one({'conversation_id': conversation_id})
        conversacion_serializada = serializar_documento(conversacion)

        return jsonify({
            'success': True,
            'conversation_id': conversation_id,
            'conversation': conversacion_serializada
        })

    except Exception as e:
        print(f"Error creando conversación: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/health')
def health():
    mongodb_status = "disconnected"
    if db is not None:
        try:
            db.client.admin.command('ping')
            mongodb_status = "connected"
        except:
            mongodb_status = "error"

    return jsonify({
        'status': 'OK',
        'claude_active': bool(claude_client),
        'mongodb_status': mongodb_status,
        'timestamp': datetime.now().isoformat()
    })


if __name__ == '__main__':
    print("Iniciando Claude Chat API con MongoDB...")
    print("Chat en: http://localhost:5000/chat")
    print("Conversaciones en: http://localhost:5000/conversations")
    print(f"Claude: {'Activo' if claude_client else 'Inactivo'}")
    print(f"MongoDB: {'Conectado' if db is not None else 'Desconectado'}")

    app.run(debug=True, host='0.0.0.0', port=5000)