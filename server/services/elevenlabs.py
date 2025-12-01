"""
Servicio de Speech-to-Text de ElevenLabs para streaming en tiempo real.
Integración con FastAPI WebSocket.
"""
import asyncio
import base64
import logging
from typing import Callable, Optional

from elevenlabs import AudioFormat, CommitStrategy, ElevenLabs, RealtimeAudioOptions


class ElevenLabsSTTService:
    """
    Wrapper sobre el SDK de ElevenLabs para manejar Speech-to-Text en tiempo real.
    
    Architecture Note:
        Este servicio está diseñado para desacoplar la lógica de WebSocket de FastAPI
        de la lógica de conexión con ElevenLabs. Utiliza un patrón de productor-consumidor
        con colas asyncio para manejar el flujo de audio entrante y transcripciones salientes
        de manera que no bloquee la recepción de audio ante latencias de red o procesamiento.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_id: str = "scribe_v2_realtime",
        language_code: str = "es",
        sample_rate: int = 16000,
        vad_silence_threshold_secs: float = 1.0,
        vad_threshold: float = 0.3,
        min_speech_duration_ms: int = 100,
        min_silence_duration_ms: int = 100,
        include_timestamps: bool = False,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Inicializa el servicio de Speech-to-Text de ElevenLabs con configuración detallada.

        Este constructor configura los parámetros esenciales para la conexión WebSocket y
        el comportamiento del algoritmo de detección de actividad de voz (VAD).

        Args:
            api_key (Optional[str]): Clave de API de ElevenLabs. Si no se proporciona,
                se intentará buscar en las variables de entorno.
            model_id (str): Identificador del modelo a utilizar. Por defecto "scribe_v2_realtime".
            language_code (str): Código de idioma ISO (ej. "es", "en"). Por defecto "es".
            sample_rate (int): Frecuencia de muestreo del audio de entrada en Hz.
                Valores soportados típicos: 16000, 24000, 44100. Por defecto 16000.
            vad_silence_threshold_secs (float): Duración del silencio (en segundos) necesaria
                para considerar que una frase ha terminado y realizar un commit.
                Un valor más bajo reduce la latencia pero puede cortar frases. Por defecto 1.0.
            vad_threshold (float): Umbral de probabilidad (0.0 a 1.0) para detectar voz.
                Valores más altos hacen el VAD menos sensible al ruido. Por defecto 0.3.
            min_speech_duration_ms (int): Duración mínima de voz (ms) para iniciar un segmento.
                Ayuda a ignorar ruidos cortos. Por defecto 100.
            min_silence_duration_ms (int): Duración mínima de silencio (ms) para separar segmentos.
                Por defecto 100.
            include_timestamps (bool): Si es True, solicita timestamps a nivel de palabra
                en las respuestas. Útil para alineación. Por defecto False.
            logger (Optional[logging.Logger]): Instancia de logger personalizada.
                Si es None, se crea uno por defecto.
        """
        self.model_id = model_id
        self.language_code = language_code
        self.sample_rate = sample_rate
        
        # Configuración de VAD (Voice Activity Detection)
        # Estos valores determinan cuándo el modelo considera que el usuario ha dejado de hablar
        # y debe procesar la frase completa (commit).

        self.vad_silence_threshold_secs = vad_silence_threshold_secs
        self.vad_threshold = vad_threshold
        self.min_speech_duration_ms = min_speech_duration_ms
        self.min_silence_duration_ms = min_silence_duration_ms
        self.include_timestamps = include_timestamps
        
        # Configuración del logger para trazabilidad y depuración
        self.logger = logger or logging.getLogger(__name__)
        
        # Inicialización del cliente de ElevenLabs
        # Se realiza de forma lazy (perezosa) para no bloquear el constructor con operaciones de red
        self.client = ElevenLabs(api_key=api_key) if api_key else ElevenLabs()
        
        self.connection = None
        self._is_connected = False
        self._should_reconnect = {"value": False}
        
    @property
    def audio_format(self) -> AudioFormat:
        """
        Obtiene el formato de audio correspondiente a la frecuencia de muestreo configurada.

        Realiza un mapeo seguro del `sample_rate` configurado en la inicialización
        hacia el enumerado `AudioFormat` del SDK de ElevenLabs.
        Se asume codificación PCM de 16 bits, que es el estándar esperado por el modelo Scribe.

        Returns:
            AudioFormat: El formato de audio de ElevenLabs correspondiente (ej. AudioFormat.PCM_16000).
                Si el sample rate no está explícitamente mapeado, retorna PCM_16000 por defecto.
        """
        # Diccionario de mapeo de frecuencias de muestreo a formatos de ElevenLabs
        format_map = {
            16000: AudioFormat.PCM_16000,
            22050: AudioFormat.PCM_22050,
            24000: AudioFormat.PCM_24000,
            44100: AudioFormat.PCM_44100,
        }
        # Devuelve el formato correspondiente o PCM_16000 por defecto si no se encuentra
        return format_map.get(self.sample_rate, AudioFormat.PCM_16000)

    async def connect(self) -> None:
        """
        Establece el WebSocket bidireccional con ElevenLabs.
        
        Nota: Esta operación es asíncrona y puede lanzar excepciones de red o autenticación
        que deben ser manejadas por el caller.
        """
        if self._is_connected:
            self.logger.warning("Intento de reconexión sobre una conexión activa ignorado.")
            return

        try:
            self.logger.info(f"Conectando a ElevenLabs STT (modelo: {self.model_id}, idioma: {self.language_code})")
            
            # RealtimeAudioOptions define la configuración de la sesión de streaming.
            # CommitStrategy.VAD es crucial: deja que el modelo decida cuándo una frase ha terminado
            # basándose en el silencio, en lugar de tener que enviar un commit manual.
            self.connection = await self.client.speech_to_text.realtime.connect(
                RealtimeAudioOptions(
                    model_id=self.model_id,
                    language_code=self.language_code,
                    audio_format=self.audio_format,
                    sample_rate=self.sample_rate,
                    commit_strategy=CommitStrategy.VAD,
                    vad_silence_threshold_secs=self.vad_silence_threshold_secs,
                    vad_threshold=self.vad_threshold,
                    min_speech_duration_ms=self.min_speech_duration_ms,
                    min_silence_duration_ms=self.min_silence_duration_ms,
                    include_timestamps=self.include_timestamps,
                )
            )
            
            self._is_connected = True
            self.logger.info("✓ Conexión establecida con ElevenLabs STT")
            
        except Exception as e:
            self.logger.error(f"Error conectando a ElevenLabs STT: {e}")
            raise

    async def disconnect(self) -> None:
        """
        Cierre limpio de recursos.
        Es importante llamar a esto para evitar fugas de conexiones WebSocket.
        """
        if self.connection and self._is_connected:
            try:
                await self.connection.close()
                self.logger.info("Conexión cerrada con ElevenLabs STT")
            except Exception as e:
                self.logger.error(f"Error cerrando conexión: {e}")
            finally:
                self._is_connected = False
                self.connection = None

    def register_handlers(
        self,
        on_partial_transcript: Optional[Callable[[str], None]] = None,
        on_final_transcript: Optional[Callable[[str], None]] = None,
        on_transcript_with_timestamps: Optional[Callable[[dict], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Registra callbacks para el patrón Observer del SDK de ElevenLabs.
        
        El SDK usa un sistema de eventos (connection.on) en lugar de async iterators.
        Aquí mapeamos esos eventos a callbacks de Python estándar.
        """
        if not self.connection:
            raise RuntimeError("Debe conectar antes de registrar handlers")

        # Handler para session_started: Confirma la conexión y retorna configuración de sesión
        def handle_session_started(data):
            self.logger.info(f"🎬 Sesión ElevenLabs iniciada: {data}")
        
        def handle_close():
            self.logger.info("🔒 Conexión WebSocket ElevenLabs cerrada")
            if on_close:
                on_close()
        
        # Registrar handler de session_started
        self.connection.on("session_started", handle_session_started)
        self.connection.on("close", handle_close)

        # Handler para transcripciones parciales (streaming en tiempo real)
        if on_partial_transcript:
            def handle_partial(data):
                # Intentar obtener texto de 'text' (nuevo formato) o 'transcript' (legacy/alternativo)
                transcript = data.get("text") or data.get("transcript", "")
                self.logger.info(f"📩 partial_transcript recibido: '{transcript}' (len={len(transcript)})")
                # Importante: Enviamos incluso strings vacíos para mantener el estado de "silencio" o "inicio de frase"
                try:
                    on_partial_transcript(transcript)
                except Exception as e:
                    self.logger.error(f"Error en handler de transcripción parcial: {e}")
            
            self.connection.on("partial_transcript", handle_partial)

        # Handler para transcripciones finales (frase completada según VAD)
        if on_final_transcript:
            def handle_committed(data):
                # Intentar obtener texto de 'text' (nuevo formato) o 'transcript' (legacy/alternativo)
                transcript = data.get("text") or data.get("transcript", "")
                self.logger.info(f"📩 committed_transcript recibido: '{transcript}' (len={len(transcript)})")
                try:
                    on_final_transcript(transcript)
                except Exception as e:
                    self.logger.error(f"Error en handler de transcripción final: {e}")
            
            self.connection.on("committed_transcript", handle_committed)

        # Handler enriquecido con timestamps (útil para subtítulos o alineación)
        if on_transcript_with_timestamps:
            def handle_committed_timestamps(data):
                # Intentar obtener texto de 'text' (nuevo formato) o 'transcript' (legacy/alternativo)
                transcript = data.get("text") or data.get("transcript", "")
                words = data.get("words", [])
                self.logger.info(f"📩 committed_transcript_with_timestamps recibido: '{transcript}' (len={len(transcript)}, words={len(words)})")
                try:
                    on_transcript_with_timestamps(data)
                except Exception as e:
                    self.logger.error(f"Error en handler de transcripción con timestamps: {e}")
            
            self.connection.on("committed_transcript_with_timestamps", handle_committed_timestamps)

        # Manejo de errores oficiales de ElevenLabs
        if on_error:
            # auth_error: Error de autenticación (API key inválida o faltante)
            def handle_auth_error(data):
                self.logger.error(f"❌ Auth error ElevenLabs: {data}")
                on_error(f"Auth error: {data}")
            
            # quota_exceeded: Límite de uso alcanzado (cuota de cuenta agotada)
            def handle_quota(data):
                self.logger.error(f"❌ Quota exceeded ElevenLabs: {data}")
                on_error(f"Quota exceeded: {data}")
            
            # transcriber_error: Error del motor de transcripción (fallo interno de transcripción)
            def handle_transcriber_error(data):
                error_msg = data.get("error", "Transcription engine error")
                self.logger.error(f"❌ Transcriber error ElevenLabs: {error_msg}")
                on_error(f"Transcriber error: {error_msg}")
            
            # input_error: Formato de entrada inválido (mensajes malformados o audio inválido)
            def handle_input_error(data):
                error_msg = data.get("error", "Invalid input format")
                self.logger.error(f"❌ Input error ElevenLabs: {error_msg}")
                on_error(f"Input error: {error_msg}")
            
            # error: Error genérico del servidor (fallo inesperado del servidor)
            def handle_error(data):
                error_msg = data.get("error", "Unknown server error")
                self.logger.error(f"❌ Generic server error ElevenLabs: {error_msg}")
                try:
                    on_error(error_msg)
                except Exception as e:
                    self.logger.error(f"Error en handler de error: {e}")
            
            # Registrar todos los handlers de error oficiales
            self.connection.on("auth_error", handle_auth_error)
            self.connection.on("quota_exceeded", handle_quota)
            self.connection.on("transcriber_error", handle_transcriber_error)
            self.connection.on("input_error", handle_input_error)
            self.connection.on("error", handle_error)

    async def send_audio(self, audio_bytes: bytes) -> None:
        """
        Envía un chunk de audio crudo al servicio.
        
        El SDK espera un payload JSON con el audio codificado en Base64.
        Esto añade un overhead de ~33% al tamaño del payload, tenerlo en cuenta para el ancho de banda.
        """
        if not self._is_connected or not self.connection:
            raise RuntimeError("No hay conexión activa. Llame a connect() primero")

        try:
            # Codificación Base64 requerida por el protocolo de ElevenLabs
            audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
            
            await self.connection.send({"audio_base_64": audio_base64})
            
        except Exception as e:
            self.logger.error(f"Error enviando audio: {e}")
            raise

    async def streaming_recognize(
        self,
        audio_queue: asyncio.Queue,
        transcript_queue: asyncio.Queue,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """
        Loop principal de procesamiento (Main Loop) con soporte de reconexión automática.
        
        Implementa el patrón Productor-Consumidor y maneja la resiliencia de la conexión:
        1. Gestiona el ciclo de vida de la conexión WebSocket (connect/disconnect).
        2. Consume audio de audio_queue y lo envía a ElevenLabs.
        3. Recibe eventos de ElevenLabs y los pone en transcript_queue.
        4. Implementa Exponential Backoff para reconexiones en caso de fallo.
        """
        if stop_event is None:
            stop_event = asyncio.Event()

        # Loop externo para manejar intentos de reconexión y reinicio de sesión
        while not stop_event.is_set():
            try:
                # 1. Conexión inicial (o reconexión)
                if not self._is_connected:
                    await self.connect()

                # Evento local para detectar caída de esta sesión específica
                session_reconnect_event = asyncio.Event()
                self._should_reconnect["value"] = False

                # --- Definición de Callbacks ---
                # Puentean eventos del SDK hacia la cola de salida y detectan errores
                
                def on_final(transcript: str):
                    self.logger.info(f"✅ Transcripción FINAL: '{transcript}'")
                    asyncio.create_task(transcript_queue.put({
                        "type": "final", "transcript": transcript, "is_final": True
                    }))

                def on_partial(transcript: str):
                    self.logger.info(f"💬 Transcripción PARCIAL: '{transcript}'")
                    asyncio.create_task(transcript_queue.put({
                        "type": "partial", "transcript": transcript, "is_final": False
                    }))

                def on_with_timestamps(data: dict):
                    transcript = data.get("text") or data.get("transcript", "")
                    words = data.get("words", [])
                    if not transcript and words:
                        transcript = "".join(word.get("text", "") for word in words)
                    
                    asyncio.create_task(transcript_queue.put({
                        "type": "final_with_timestamps",
                        "transcript": transcript,
                        "words": words,
                        "is_final": True
                    }))

                def on_error_handler(error: str):
                    self.logger.error(f"Error STT reportado por SDK: {error}")
                    asyncio.create_task(transcript_queue.put({"type": "error", "error": error}))
                    # Marcar para reconexión
                    self._should_reconnect["value"] = True
                    session_reconnect_event.set()

                def on_close_handler():
                    self.logger.warning("Detectado cierre de conexión (on_close)")
                    session_reconnect_event.set()

                # 2. Registro de Handlers
                self.logger.info("Registrando handlers para nueva sesión...")
                self.register_handlers(
                    on_partial_transcript=on_partial,
                    on_final_transcript=on_final if not self.include_timestamps else None,
                    on_transcript_with_timestamps=on_with_timestamps if self.include_timestamps else None,
                    on_error=on_error_handler,
                    on_close=on_close_handler,
                )

                # 3. Loop de Envío de Audio (Inner Loop)
                audio_chunks_processed = 0
                self.logger.info("Iniciando loop de transmisión de audio...")
                
                while not stop_event.is_set() and not session_reconnect_event.is_set():
                    # Usamos asyncio.wait para esperar audio O eventos de control (stop/reconnect)
                    # Esto evita quedarnos bloqueados en audio_queue.get() si la conexión se cae
                    
                    get_audio_task = asyncio.create_task(audio_queue.get())
                    wait_stop = asyncio.create_task(stop_event.wait())
                    wait_reconnect = asyncio.create_task(session_reconnect_event.wait())
                    
                    done, pending = await asyncio.wait(
                        [get_audio_task, wait_stop, wait_reconnect],
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    # Cancelar tareas pendientes para no dejar corrutinas huérfanas
                    for task in pending:
                        task.cancel()

                    if wait_stop in done:
                        break # Salida limpia solicitada por usuario
                    
                    if wait_reconnect in done:
                        self.logger.warning("Interrupción del loop de audio por evento de reconexión")
                        break # Salida por error de conexión
                    
                    if get_audio_task in done:
                        try:
                            audio_chunk = get_audio_task.result()
                            audio_chunks_processed += 1
                            if audio_chunks_processed % 50 == 0:
                                self.logger.info(f"🎤 Audio procesado: {audio_chunks_processed} chunks")
                            
                            await self.send_audio(audio_chunk)
                        except Exception as e:
                            self.logger.error(f"Error enviando audio: {e}")
                            self._should_reconnect["value"] = True
                            break

                # 4. Lógica de Reconexión (Exponential Backoff)
                if stop_event.is_set():
                    break # Salir del loop externo

                if self._should_reconnect["value"] or session_reconnect_event.is_set():
                    self.logger.info("Iniciando secuencia de recuperación de conexión...")
                    await self.disconnect() # Limpieza previa
                    
                    reconnected = False
                    for attempt in range(1, 4): # 3 intentos
                        if stop_event.is_set(): break
                        
                        backoff_time = 2 ** (attempt - 1) # 1s, 2s, 4s
                        self.logger.info(f"⏳ Reintentando conexión en {backoff_time}s (Intento {attempt}/3)...")
                        await asyncio.sleep(backoff_time)
                        
                        try:
                            await self.connect()
                            reconnected = True
                            self.logger.info("♻️ Reconexión exitosa, reanudando transmisión.")
                            break
                        except Exception as e:
                            self.logger.error(f"Fallo reconexión {attempt}: {e}")
                    
                    if not reconnected:
                        self.logger.error("❌ Se agotaron los intentos de reconexión. Abortando servicio.")
                        asyncio.create_task(transcript_queue.put({
                            "type": "error",
                            "error": "Max reconnection attempts reached. Service unavailable."
                        }))
                        break # Salir del loop externo

            except Exception as e:
                self.logger.error(f"Error crítico no manejado en streaming_recognize: {e}")
                # Breve pausa para evitar busy-loop en caso de error persistente inmediato
                await asyncio.sleep(1)
                if stop_event.is_set(): break

        # Cleanup final
        await self.disconnect()

    async def __aenter__(self):
        """Context manager entry"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        await self.disconnect()
