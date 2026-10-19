# Estudio de voz

Servidor local con interfaz web y tres herramientas:

- **Clonar voz** con Chatterbox: tu voz leyendo cualquier texto, en español o inglés.
- **Voces**: crea voces nuevas sin grabar a nadie (como las de ElevenLabs) y
  guárdalas para usarlas en *Clonar voz*.
- **Transcribir** con WhisperX: de audio o vídeo a texto, con tiempos por palabra y
  subtítulos SRT/VTT.

En las dos eliges **idioma de entrada y de salida**; si no coinciden, se traduce
automáticamente (en tu equipo, con MarianMT).

## Arrancar

Doble clic en **`iniciar.bat`** → se abre **http://127.0.0.1:7860**.

La primera vez instala todo (tarda bastante y descarga varios GB). Necesitas
**Python 3.11** (`winget install Python.Python.3.11`). Se crean dos entornos:

| Carpeta | Para qué |
|---|---|
| `.venv` | Chatterbox (clonar voz) |
| `.venv-whisper` | WhisperX (transcribir) |
| `.venv-kokoro` | Kokoro (voces en inglés) |
| `.venv-qwen` | Qwen3-TTS (diseñar voces) |

Van separados porque cada uno pide versiones distintas de PyTorch. Si algo
falló al instalarse: `instalar-whisper.bat` (transcribir) o
`instalar-voces.bat` (pestaña Voces).

Cada modelo se descarga la primera vez que se usa (voz, Whisper, traductor).

## Clonar voz

1. **Elige tu voz**: sube un audio o grábalo. Lo ideal: 10–20 s, sin música ni eco.
2. **Escribe el texto** y elige **Escribo en → Audio en** (Español / Inglés).
   Si son distintos, el texto se traduce antes de generar la voz y el historial
   muestra el original.
3. **Descarga** el WAV.

Si la voz en inglés suena con acento español (o al revés), baja *Ritmo /
fidelidad (CFG)* a 0 en los ajustes.

**Guiones de vídeo:** marca *Un audio por línea* y escribe `nombre | texto`:

```
gancho | Diecinueve toneladas y media... y aun así gira en una rotonda.
tamano | Un autobús normal mide doce metros.
```

Obtienes `gancho.wav`, `tamano.wav`… y un ZIP con todo y un `duraciones.json`.

## Voces (crear voces nuevas)

Para tener voces "de stock" que puedas usar en tus vídeos sin que sean la tuya.

| Idioma | Modelo | Cómo |
|---|---|---|
| **Inglés** | **Kokoro-82M** | *Voces listas*: 13 voces (EE. UU. y Reino Unido). Muy natural, rapidísimo, va con la CPU. |
| **Español** (y inglés a medida) | **Qwen3-TTS 1.7B VoiceDesign** | *Diseñar*: describes la voz y la inventa. Plantillas: narrador, locutora, tráiler… |

Los dos tienen licencia **Apache 2.0**: puedes usarlos en anuncios.
No hay un modelo abierto solo de español que suene natural; Qwen3-TTS es el que
mejor lo habla. Escribe la descripción en español o en inglés (se traduce sola).

1. Elige idioma y voz (o describe la que quieres y el acento: España / Latinoamérica).
2. **Generar prueba** con el texto de ejemplo (15–20 s).
3. ¿Te gusta? **Guardar como voz**. Si no, pulsa 🎲 (misma descripción, otra voz).
4. En *Clonar voz* ya aparece seleccionada: Chatterbox la usará para todos tus
   guiones y sonará siempre igual.

**Memoria:** Qwen necesita unos 5 GB de gráfica libres. Si no los hay (por
ejemplo una RTX 3050 de 4 GB), lo hace con el procesador: unos minutos por
prueba, pero solo hace falta una vez por voz. En gráficas de 6–8 GB, Chatterbox
se pone en pausa mientras diseñas y se recarga solo al volver a generar.

## Transcribir

1. Arrastra un **audio o vídeo** (MP3, WAV, M4A, MP4…) o graba.
2. **El audio está en**: Detectar / Español / Inglés.
   **Quiero el texto en**: El mismo / Español / Inglés.
3. Modelo: *Rápido*, *Equilibrado* (recomendado) o *Máxima precisión*.
4. Descarga **TXT, SRT o VTT** del original y de la traducción, o el **JSON** con
   el tiempo de cada palabra (útil para subtítulos animados en Remotion).

**Usar para clonar** pasa el texto a la pestaña de voz con los idiomas ya puestos:
por ejemplo, transcribes un audio en inglés, lo traduces al español y lo
generas con tu voz.

La GPU se usa para una cosa a la vez: mientras transcribe no se puede generar
voz, y al revés. Si la GPU no puede con la transcripción (memoria o CUDA),
se repite automáticamente con el procesador y la web te avisa.

## Opciones

```powershell
iniciar.bat --dispositivo cpu   # todo con el procesador (si la GPU no da)
iniciar.bat --puerto 8000       # otro puerto
iniciar.bat --simular           # probar la interfaz sin cargar modelos
iniciar-en-red.bat              # usarlo desde otro equipo de tu casa
```

**En red:** abre `iniciar-en-red.bat` en el portátil, acepta el aviso del
firewall (solo redes privadas) y entra desde el otro equipo a la dirección que
muestra la consola. No tiene contraseña: solo en tu red de casa.

**Grabar desde otro equipo:** los navegadores solo dejan usar el micrófono en
páginas seguras. Por eso `iniciar-en-red.bat` abre además una dirección
**https en el puerto siguiente** (por ejemplo `https://192.168.1.216:7861`) con
un certificado propio que se crea solo en la carpeta `certificado`. La primera
vez el navegador avisa de que no es oficial: «Avanzado» → «Continuar». Si
cambia la IP del equipo, el certificado se rehace solo (y vuelve a avisar).

## Requisitos

- Tarjeta NVIDIA con 4 GB o más. En una RTX 3050 de portátil cierra antes
  programas que usen la GPU (navegador con muchas pestañas, Discord…).
- Unos 8 GB de RAM libres (16 GB para diseñar voces con la CPU) y ~22 GB de
  disco para los entornos y los modelos.

## Nota

Chatterbox añade una marca de agua inaudible (Perth) a lo que genera. Úsalo con
tu voz, con la de alguien que te haya dado permiso o con las voces que crees
en la pestaña *Voces*.
