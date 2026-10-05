# PiKVM DIY: audio HDMI y micrófono USB con Raspberry Pi 4 + C792

Estado: **base experimental de integración, pendiente de prueba en hardware**.

Este directorio prepara un fork de `pikvm/kvmd` para hacer reproducible el uso
de audio bidireccional en un montaje Raspberry Pi 4 + C792/TC358743. Reutiliza
la interfaz de PiKVM, Janus, el plugin de µStreamer, ALSA y el dispositivo USB
compuesto de KVMD.

La primera entrega incluye ejemplos de configuración, inventario de solo
lectura y un protocolo de prueba. No es una imagen instalable ni un instalador.
Los ejemplos deben contrastarse con las versiones instaladas antes de aplicarse.

## Punto de partida

- El propietario del montaje confirmó que escucha audio usando BliKVM.
- Con PiKVM ya había utilizado vídeo, teclado y mouse en la misma Pi 4/C792.
- Todavía no hay una prueba confirmada del micrófono desde el navegador hasta
  el ThinkPad, ni de ambas direcciones simultáneas con esta base.
- Se mantendrá el acceso por LAN/WireGuard y el monitor conectado a la C792.

La documentación oficial de PiKVM limita el soporte de audio a V3/V4 y excluye
DIY. El código público inspeccionado sí contiene las piezas de transporte,
captura y micrófono USB. Esto justifica una prueba de integración; no permite
declarar compatible cualquier imagen o cualquier capturadora DIY.

## Las dos rutas de audio

| Función | Recorrido |
| --- | --- |
| Escuchar el ThinkPad | HDMI del ThinkPad → C792 → I²S → ALSA `tc358743` → Janus/Opus/WebRTC → navegador |
| Hablar desde el navegador | Micrófono local → WebRTC/Opus → Janus/µStreamer → reproducción ALSA `UAC2Gadget` → USB → micrófono en el ThinkPad |

La ruta del micrófono usa USB. No necesita regresar por HDMI ni introducir
un micrófono físico en la C792. Se integra en el dispositivo USB compuesto
que ya gestiona teclado, mouse y almacenamiento virtual.

La salida de reproducción ALSA de la Raspberry se convierte en una entrada
de grabación para el host USB. Por eso `aplay.device` y los atributos `p_*`
del gadget corresponden aquí al micrófono que ve Windows.

## Código de referencia inspeccionado

Consulta `upstream-refs.json` para los commits exactos. Son referencias de
análisis, no una afirmación sobre las versiones instaladas en el dispositivo.

| Componente | Archivo y responsabilidad |
| --- | --- |
| KVMD | `kvmd/apps/otg/__init__.py`: crea `uac2.usb0` dentro del gadget existente |
| KVMD | `kvmd/apps/_scheme.py`: opciones `otg.devices.audio` |
| KVMD | `web/share/js/kvm/stream_janus.js`: selección de micrófono, permisos, medidores y negociación |
| KVMD | `configs/janus/janus.plugin.ustreamer.jcfg`: fuente de captura y destino de micrófono |
| µStreamer | `janus/src/acap.c`: captura ALSA y codificación |
| µStreamer | `janus/src/client.c`: recepción y decodificación de Opus |
| µStreamer | `janus/src/plugin.c`: negociación y escritura PCM en ALSA |
| µStreamer | `janus/src/au.c`: detección del dispositivo ALSA |

No se requieren inicialmente otro servidor de audio ni una interfaz web nueva.
Solo se modificarán estas piezas si una prueba aislada identifica una carencia.

## 1. Guardar la referencia que funciona en BliKVM

Desde la raíz de este checkout, ejecutar en la Raspberry que corre BliKVM:

```sh
python3 contrib/c792-audio/doctor.py > /tmp/c792-blikvm.json
```

El comando crea únicamente el archivo indicado por la redirección. El script
inspecciona el sistema sin grabar, reproducir, activar el micrófono, reiniciar
servicios o cambiar la configuración. Requiere Python 3.9 o posterior y no
instala dependencias. Si faltan permisos, el informe los identifica; puede
ejecutarse como root para completar las lecturas.

No recopila archivos de contraseñas ni configuración completa de autenticación.
Los datos de Janus se limitan a los parámetros de medios admitidos. El informe
es inventario: no certifica que se escuche sonido ni que el micrófono llegue al
host. La observación del usuario se registra por separado.

### Referencia recibida de BliKVM (2026-10-05)

El inventario ejecutado en el montaje aporta estos datos. Son observaciones de
BliKVM, no resultados de una prueba de esta base sobre PiKVM.

| Componente | Dato observado |
| --- | --- |
| Plataforma | Raspberry Pi 4 Model B Rev 1.4, `aarch64` |
| Paquetes | `blikvm 2.2.0-alpha`, `alsa-utils 1.2.8-1+rpt1` |
| Kernel | `6.12.34+rpt-rpi-v8` |
| Arranque | `dtoverlay=tc358743-audio` sin comentar en `/boot/firmware/config.txt` |
| Captura ALSA | `tc358743`, tarjeta 1, dispositivo 0 |
| Reproducción ALSA hacia USB | `UAC2Gadget`, tarjeta 4, dispositivo 0 |
| Función USB | `g1/uac2.usb0`, enlazada a una configuración y vinculada al UDC |

El gadget informa `p_chmask=3`, `p_srate=48000` y `p_ssize=2`: dos canales,
48 kHz y 16 bits en la dirección de micrófono del host. Coincide con el perfil
previsto para KVMD. `c_chmask=0` desactiva la dirección de altavoces USB; su
`c_srate=64000` no es la frecuencia del HDMI ni un ajuste que deba corregirse.
Los índices de tarjetas anteriores describen esa ejecución, no identificadores
que deban fijarse en la configuración.

Las lecturas principales se completaron sin errores de permisos. La ausencia
de archivos y enlaces propios de KVMD es coherente con estar ejecutando BliKVM.
La consulta conjunta de paquetes recuperó las versiones indicadas y devolvió
código 1, compatible con que algunos de los nombres consultados estén ausentes.
Los marcadores de verificación en
`false` siguen significando que el inventario no realiza pruebas de sonido.

La primera revisión del diagnóstico no incluía la ruta del paquete oficial
[BliKVM v2.2.0-alpha](https://github.com/blikvm/blikvm/blob/1401a6a998a6a3aa3b1b0cc377a62dd8440c197c/script/packdeb.sh):
`/mnt/exec/release/lib/pi/janus_configs/janus.plugin.ustreamer.jcfg`.
Esa ruta ahora se consulta por defecto; la primera revisión también puede
leerla mediante `--janus-config`. Falta obtener sus valores en este equipo:
la ubicación en el empaquetado no acredita los parámetros instalados ni el
commit del plugin en ejecución.

También sigue pendiente comprobar que Windows enumere el micrófono USB y que
su medidor responda a la voz enviada desde el navegador. La presencia del
gadget en la Raspberry no demuestra por sí sola la recepción en el ThinkPad.

Conservar también la versión de BliKVM que muestra su interfaz. En la imagen
PiKVM que se destine a pruebas, repetir la lectura:

```sh
python3 contrib/c792-audio/doctor.py > /tmp/c792-pikvm.json
```

## 2. Perfil a contrastar con PiKVM

### Captura de la C792

`boot-extra.txt.example` contiene el overlay adicional de audio. Es un fragmento
para integrar una sola vez en el archivo de arranque activo, en una sección
aplicable a la Pi 4. Se conservan las opciones existentes de vídeo y USB OTG.

No cambiar el cableado I²S que ya funciona con BliKVM. No copiar el perfil
completo V3: incluye GPIO y periféricos propios de esa placa. No activar
`4lane=1` en este montaje de Pi 4.

Tras preparar una imagen de prueba, comprobar que ALSA ofrece la tarjeta
`tc358743` y que el usuario del servicio Janus puede acceder a ella. El índice
numérico de la tarjeta puede variar entre arranques o imágenes.

El EDID debe anunciar audio para que Windows lo envíe por HDMI. La herramienta
de PiKVM admite `kvmd-edidconf --set-audio=yes`; este comando modifica el EDID
y debe aplicarse únicamente en la imagen de prueba tras guardar su original.
Se conserva el EDID específico del montaje, sin sustituirlo por un preset V3.

### Configuración de Janus

`janus-audio.jcfg.example` muestra los bloques de audio para la versión de
µStreamer inspeccionada. Integrar los campos en los bloques existentes del
archivo activo; no sustituir toda la configuración ni duplicar bloques.
El `video.sink` ya configurado debe seguir coincidiendo con el de µStreamer.

En el código de referencia se usan `acap` y `aplay`. Hay guías antiguas que
usan `audio` y `memsink`, y distribuciones con configuraciones diferentes.
La compatibilidad se determina con la versión del plugin instalado.

**Detalle que puede explicar detecciones fallidas:** `us_au_probe()` del commit
inspeccionado espera nombres como `hw:tc358743,0` o `plughw:UAC2Gadget,0`.
Una cadena que ALSA acepta, como `hw:CARD=tc358743`, no pasa esa comprobación
concreta. El fallo debe confirmarse con la imagen instalada; no se atribuyen a
este detalle los errores históricos de autenticación o WebRTC sin evidencia.

La frecuencia de captura se obtiene del TC358743. No se fuerza a 48 kHz una
señal HDMI que pueda usar otra frecuencia; el transporte realiza su conversión.
El ejemplo fija `sampling_rate = 0` para seleccionar esa detección y reemplazar
cualquier frecuencia fija heredada del bloque `acap` existente.

### Micrófono USB

`override-audio.yaml.example` declara únicamente la función de micrófono USB.
Se debe fusionar bajo el árbol `otg` existente de la configuración de PiKVM,
sin reemplazar el archivo completo. El bloque `speakers.enabled: false` se
refiere a altavoces **USB**; no desactiva el audio HDMI que se escucha.

KVMD crea el gadget de micrófono con dos canales a 48 kHz y 16 bits. Se usa
su propia gestión OTG: no cargar un gadget `g_audio` independiente que compita
con teclado y mouse por el controlador USB.

Revisar los dispositivos activos y endpoints con `kvmd-otgconf`. El preset
DIY V2 habitual utiliza cuatro de nueve endpoints; el micrófono añade dos.
El consumo real depende de los dispositivos que tenga habilitados el montaje.
Cambiar la composición USB provoca una nueva enumeración y requiere comprobar
que teclado y mouse vuelven a funcionar.

## 3. Pruebas que permiten aceptar el soporte

| Prueba | Evidencia exigida |
| --- | --- |
| Captura HDMI | Sonido continuo y sonidos cortos llegan al navegador con la salida HDMI elegida en Windows |
| Enumeración USB | Windows muestra un micrófono USB; la Raspberry ofrece `UAC2Gadget` |
| USB aislado | Una muestra conocida enviada a la reproducción ALSA del gadget llega al medidor/grabadora de Windows |
| Micrófono web | Hablar en el micrófono seleccionado mueve el medidor y permite una grabación inteligible en Windows |
| Conversación | Una llamada de prueba recibe y transmite simultáneamente; se mide la demora percibida |
| Silencio y reconexión | Se recupera tras silencio prolongado, recarga del navegador, reconexión USB/HDMI y reinicio del host |
| Regresión | Teclado, mouse, vídeo, acceso BIOS y almacenamiento virtual siguen funcionando cuando se utilizan |
| Navegadores y red | Firefox y Chrome en LAN; después el acceso WireGuard real |
| Ciclo del micrófono | Activar, cambiar dispositivo, silenciar y cerrar la sesión detiene el envío como corresponde |

En la aplicación del ThinkPad se selecciona el micrófono USB como entrada y
HDMI como salida. Para usar la entrada en una llamada no se necesita activar
“Escuchar este dispositivo” de Windows. Empezar con audífonos simplifica la
prueba de eco.

El navegador necesita permiso de micrófono y un contexto seguro. La aceptación
del dispositivo USB y el comportamiento en Windows se prueban en el equipo
real; un test en un contenedor Linux no los reemplaza.

Mantener la microSD de BliKVM que ya funciona y utilizar la otra microSD para
PiKVM permite volver a la referencia conocida durante la integración.

## Desarrollo y entrega

El repositorio inicial a bifurcar es **`pikvm/kvmd`**. `pikvm/pikvm` es el
repositorio principal de documentación; el daemon, la interfaz y los perfiles
están en KVMD. `pikvm/ustreamer` solo necesita un fork adicional si aparece un
defecto del transporte o de la detección que requiera corregir código.

La primera rama propuesta es `feat/c792-two-way-audio`. Este directorio puede
incorporarse a esa rama y convertirse después en un perfil instalable, cuando
se conozcan las versiones que superan las pruebas anteriores. No se promete
compatibilidad de todos los commits futuros ni una imagen final con este ZIP.

Para probar el inventario en un entorno sin Raspberry:

```sh
python3 -m unittest discover -s contrib/c792-audio -p 'test_*.py' -v
```

El informe de validación de esta entrega distingue estas pruebas del diagnóstico
en hardware. La licencia y los avisos de upstream deben conservarse. Los nuevos
archivos de este directorio se distribuyen bajo GPL-3.0-or-later, como KVMD.

## Fuentes

- [PiKVM: audio y micrófono](https://docs.pikvm.org/audio/)
- [PiKVM: dispositivos y endpoints USB](https://docs.pikvm.org/usb/)
- [BliKVM: audio bidireccional](https://blikvm.com/docs/peripheral-devices/two-way-audio/)
- [BliKVM: implementación inicial, issue 330](https://github.com/blikvm/blikvm/issues/330)
- [KVMD y su configuración](https://github.com/pikvm/kvmd/tree/42df955c17bf9003adfcce01a5b5a2ec488eaf65)
- [µStreamer y plugin Janus](https://github.com/pikvm/ustreamer/tree/0e64f1fedab4030c7bf58a287c462e21919c1d48)
- [Linux: gadget UAC2](https://docs.kernel.org/usb/gadget-testing.html)
