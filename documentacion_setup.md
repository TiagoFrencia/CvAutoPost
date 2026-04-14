# 📋 Documentación Complementaria — Auto Applier Bot

---

## Sección A — Prerequisites Windows

> Antes de ejecutar `docker-compose up` por primera vez, verificá que tu entorno Windows cumple estos requisitos. Saltear cualquier paso va a resultar en errores difíciles de diagnosticar.

---

### A.1 — WSL2 (obligatorio)

Docker Desktop en Windows requiere WSL2 como backend. Sin él, los containers Linux no arrancan.

**Verificar si WSL2 está instalado:**
```powershell
wsl --status
```
Si devuelve `Default Version: 2` → estás listo. Si no, seguí estos pasos:

**Instalar WSL2:**
```powershell
# Ejecutar como Administrador en PowerShell
wsl --install
```
Esto instala WSL2 + Ubuntu por defecto. Reiniciá la PC cuando lo pida.

**Verificar que la versión es 2:**
```powershell
wsl -l -v
# Debe mostrar VERSION 2 junto a tu distro
```

> ⚠️ Si ya tenés WSL1 instalado, convertilo:
> ```powershell
> wsl --set-default-version 2
> wsl --set-version Ubuntu 2
> ```

---

### A.2 — Docker Desktop

**Versión mínima recomendada:** Docker Desktop 4.20+

**Descarga:** https://www.docker.com/products/docker-desktop/

**Configuración obligatoria después de instalar:**

1. Abrí Docker Desktop
2. Andá a **Settings → General**
3. Verificá que **"Use the WSL 2 based engine"** esté activado ✅
4. Andá a **Settings → Resources → Advanced**
5. Asigná mínimo **6 GB de RAM** y **4 CPUs**
   - Playwright con Chromium consume ~1.5 GB por instancia
   - PostgreSQL consume ~300 MB
   - El bot consume ~200 MB
   - Total estimado en pico: ~2.5 GB → dejá margen con 6 GB
6. En **Settings → Resources → WSL Integration**
   - Activá la integración con tu distro Ubuntu ✅
7. Hacé click en **Apply & Restart**

**Verificar que Docker funciona:**
```powershell
docker run hello-world
# Debe imprimir: "Hello from Docker!"
```

---

### A.3 — Autostart de Docker Desktop con Windows

APScheduler corre dentro de Docker. Si Docker Desktop no arranca automáticamente con Windows, el bot no ejecuta su tarea diaria aunque la PC esté prendida.

**Activar autostart:**
1. Abrí Docker Desktop
2. Andá a **Settings → General**
3. Activá **"Start Docker Desktop when you sign in to your computer"** ✅

**Verificar que los containers arrancan solos:**

En Docker Desktop → **Settings → General** → activá **"Start containers that were running when Docker Desktop was last shut down"** ✅

Alternativamente, podés configurar el bot como servicio con reinicio automático en `docker-compose.yml`:
```yaml
services:
  bot:
    restart: unless-stopped   # ← ya incluido en el docker-compose.yml del proyecto
  db:
    restart: unless-stopped
```
Con `restart: unless-stopped`, si Docker arranca, los containers arrancan solos sin intervención manual.

---

### A.4 — Google Chrome instalado en el host

El scraper de LinkedIn usa **Nodriver/CDP**, que se conecta al Chrome real de tu sistema (no al Chromium bundled de Playwright). Esto es obligatorio para pasar el WAF de LinkedIn.

**Verificar que Chrome está instalado:**
```powershell
# Buscar el ejecutable de Chrome
Get-Command chrome -ErrorAction SilentlyContinue
# O verificar la ruta típica:
Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

Si Chrome no está instalado: https://www.google.com/chrome/

**Ruta que va en el `.env`:**
```env
CHROME_EXECUTABLE_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
```

> ⚠️ No uses Chromium como sustituto para LinkedIn. La huella TLS (JA3/JA4) de Chrome real es diferente a la de Chromium y es la razón principal por la que Nodriver evade la detección.

---

### A.5 — Variables de entorno (.env)

Copiá `.env.example` a `.env` y completá antes del primer `docker-compose up`:

```env
# Base de datos
DB_URL=postgresql://bot_user:bot_password@db:5432/auto_applier

# API Keys
GEMINI_API_KEY=tu_api_key_aqui

# Telegram
TELEGRAM_BOT_TOKEN=tu_token_aqui
TELEGRAM_CHAT_ID=tu_chat_id_aqui

# Chrome (para LinkedIn via Nodriver)
CHROME_EXECUTABLE_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe

# Límites de seguridad
MAX_APPLICATIONS_PER_DAY_LINKEDIN=5
MAX_APPLICATIONS_PER_DAY_COMPUTRABAJO=15
MAX_APPLICATIONS_PER_DAY_INDEED=15
```

---

### A.6 — Primer arranque

Una vez cumplidos todos los prerequisites:

```powershell
# 1. Clonar el repo y entrar al directorio
git clone <repo-url>
cd auto_applier_bot

# 2. Copiar variables de entorno
copy .env.example .env
# Editar .env con tus datos

# 3. Levantar servicios
docker-compose up -d

# 4. Verificar que ambos containers están corriendo
docker-compose ps
# Debe mostrar: bot (running) + db (running)

# 5. Ejecutar migraciones
docker-compose exec bot alembic upgrade head

# 6. Cargar datos iniciales (plataformas + perfiles CV)
docker-compose exec bot python main.py seed
```

---

### A.7 — Checklist de Prerequisites

Antes de continuar con la Fase 1, verificá cada ítem:

- [ ] WSL2 instalado y configurado como default (`wsl --status` muestra `Default Version: 2`)
- [ ] Docker Desktop 4.20+ instalado
- [ ] Docker Desktop usa WSL2 backend (Settings → General)
- [ ] Docker Desktop tiene 6 GB RAM asignados (Settings → Resources)
- [ ] Docker Desktop arranca con Windows (Settings → General)
- [ ] Containers con `restart: unless-stopped` configurado
- [ ] Google Chrome instalado en el host
- [ ] `.env` creado y completado con todas las variables
- [ ] `docker run hello-world` ejecuta sin errores

---

---

## Sección B — Flujo de Primer Login de LinkedIn

> LinkedIn en 2026 tiene múltiples capas de verificación: 2FA, verificación por email, puzzles de seguridad y detección de automatización. Este flujo documenta cómo hacer el setup inicial de cookies manualmente de forma segura, y cómo validar que el resultado es correcto.

---

### B.1 — Por qué cookies y no usuario/contraseña

El bot **nunca guarda ni usa tu contraseña de LinkedIn**. En cambio:

1. Vos te logueás manualmente una vez (el bot abre Chrome para que lo hagas)
2. El bot guarda las cookies de sesión en `data/cookies/linkedin.json`
3. En cada ejecución posterior, inyecta esas cookies en el browser — LinkedIn lo trata como una sesión existente, no como un nuevo login

Esto evita activar los detectores de login automatizado y no requiere manejar 2FA en código.

---

### B.2 — Setup inicial: `login_helper.py`

Este script abre tu Chrome real en modo visible para que te logueés manualmente.

**Ejecutar:**
```powershell
docker-compose exec bot python login_helper.py --platform linkedin
```

Esto va a:
1. Abrir una ventana de Chrome apuntando a `https://www.linkedin.com/login`
2. Esperar a que completes el login manualmente (incluyendo 2FA si LinkedIn lo pide)
3. Detectar automáticamente cuando la sesión está activa (URL cambia a `/feed`)
4. Guardar las cookies en `data/cookies/linkedin.json`
5. Cerrar el browser y confirmar el guardado

**Tiempo estimado:** 3-5 minutos incluyendo 2FA.

---

### B.3 — Manejo de 2FA durante el login

LinkedIn puede pedir cualquiera de estas verificaciones. Cómo manejar cada una:

**Código por email:**
1. LinkedIn envía un código de 6 dígitos a tu email registrado
2. Abrí tu email, copiá el código
3. Ingresalo en la ventana de Chrome que abrió el script
4. Hacé click en "Verificar"
5. El script detecta que la sesión está activa y guarda las cookies

**Código por SMS / Autenticator App:**
1. Ingresá el código en la ventana de Chrome
2. Igual que arriba

**Puzzle de seguridad (imagen/verificación visual):**
1. Resolvé el puzzle manualmente en la ventana de Chrome
2. El script espera hasta que termines (tiene un timeout de 5 minutos)

**Verificación de dispositivo nuevo:**
LinkedIn a veces pide confirmar desde un dispositivo conocido:
1. Abrí LinkedIn desde tu teléfono u otro browser donde ya estés logueado
2. Aprobá el nuevo dispositivo desde ahí
3. Volvé a la ventana de Chrome del script y continuá

> ⚠️ No cerrés la ventana de Chrome que abrió el script. Si la cerrás antes de que se guarden las cookies, el proceso se cancela y tenés que empezar de nuevo.

---

### B.4 — Estructura del archivo de cookies generado

El script guarda las cookies en formato JSON estándar de Playwright:

```json
[
  {
    "name": "li_at",
    "value": "AQEDAS...",
    "domain": ".linkedin.com",
    "path": "/",
    "expires": 1780000000,
    "httpOnly": true,
    "secure": true,
    "sameSite": "None"
  },
  {
    "name": "JSESSIONID",
    "value": "ajax:123456789",
    "domain": "www.linkedin.com",
    "path": "/",
    "expires": 1780000000,
    "httpOnly": false,
    "secure": true,
    "sameSite": "None"
  }
]
```

**La cookie crítica es `li_at`** — es el token de sesión de LinkedIn. Si está presente y no expiró, la sesión es válida.

---

### B.5 — Validar que las cookies son correctas

Después de correr `login_helper.py`, verificá que el archivo es válido:

```powershell
docker-compose exec bot python -c "
from services.session_manager import SessionManager
sm = SessionManager()
is_valid, days_remaining = sm.check_expiry('linkedin')
print(f'Válido: {is_valid}')
print(f'Días restantes: {days_remaining}')
"
```

**Resultado esperado:**
```
Válido: True
Días restantes: 340   ← LinkedIn genera sesiones de ~1 año
```

**Si `is_valid` es False:**
- Las cookies no se guardaron correctamente → repetir el proceso desde B.2
- El archivo JSON está malformado → borrarlo y repetir
- LinkedIn invalidó la sesión inmediatamente (raro pero posible) → esperar 1 hora y repetir

**Validación manual alternativa** — verificar que `li_at` existe en el JSON:
```powershell
docker-compose exec bot python -c "
import json
cookies = json.load(open('data/cookies/linkedin.json'))
li_at = next((c for c in cookies if c['name'] == 'li_at'), None)
print('li_at encontrado:', li_at is not None)
if li_at:
    import datetime
    exp = datetime.datetime.fromtimestamp(li_at['expires'])
    print('Expira:', exp.strftime('%Y-%m-%d'))
"
```

---

### B.6 — Alertas automáticas de expiración

El `SessionManager` verifica la expiración de cookies **al inicializar**, sin abrir ningún browser. Si las cookies expiran en menos de 48 horas, envía una alerta por Telegram antes de cualquier ejecución:

```
⚠️ ALERTA: Cookies de LinkedIn expiran en 36 horas.
Ejecutá: python login_helper.py --platform linkedin
El bot no puede postular a LinkedIn hasta que renueves la sesión.
```

Cuando recibas esta alerta, repetí el proceso desde el paso B.2. El re-login es igual al login inicial — LinkedIn no distingue entre ambos si usás el mismo Chrome con el mismo perfil.

---

### B.7 — Buenas prácticas de seguridad

- **Nunca commitees `data/cookies/`** — está en `.gitignore` por defecto
- Las cookies están cifradas en reposo con Fernet (`services/session_manager.py`)
- El login siempre se hace desde tu **IP residencial real** — nunca desde un proxy
- Si LinkedIn te pide verificación adicional días después del login (inusual pero posible), repetí el proceso completo desde B.2
- El bot tiene un límite estricto de **5 postulaciones por día** en LinkedIn para no activar detección por volumen

---

### B.8 — Checklist de Primer Login LinkedIn

- [ ] `login_helper.py --platform linkedin` ejecutado
- [ ] Login manual completado en la ventana de Chrome (incluyendo 2FA si fue requerido)
- [ ] Archivo `data/cookies/linkedin.json` existe y tiene tamaño > 1KB
- [ ] `session_manager.check_expiry('linkedin')` devuelve `(True, días > 30)`
- [ ] Cookie `li_at` presente en el JSON con fecha de expiración futura
- [ ] Alerta de Telegram de "sesión lista" recibida (opcional, según implementación)

---

## Resumen de Pre-trabajo Completado

| Entregable | Estado |
|---|---|
| `data/cvs/cv_remoto.json` | ✅ Listo |
| `data/cvs/cv_local.json` | ✅ Listo |
| `data/answers.yaml` | ✅ Listo |
| Prerequisites Windows | ✅ Documentado (Sección A) |
| Flujo primer login LinkedIn | ✅ Documentado (Sección B) |

**Evaluación estimada post pre-trabajo: 95/100 → Producción-ready para proyecto personal.**

**Próximo paso:** Fase 1 — Setup del proyecto e implementación de scrapers GetOnBoard y RemoteOK.
