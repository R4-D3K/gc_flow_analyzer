# GC Flow Analyzer — Deployment Guide (Synology NAS)

Tento dokument popisuje kompletní postup nasazení aplikace GC Flow Analyzer
na Synology NAS s přístupem z internetu.

---

## Přehled architektury

```
Internet (HTTPS)
      │
      ▼
radekn.com (public IP)
      │
      ▼
Synology DSM — Reverse Proxy (nginx)
  gc.radekn.com:443  →  localhost:8765
      │
      ▼
Docker container: gc-flow-analyzer
  port 8000 (interní)
      │  čte z
      ▼
/volume1/docker/gc-flow-analyzer/data/orgs.yaml
  (Fernet-šifrované OAuth credentials)
```

**Použité porty:**

| Port | Účel |
|------|------|
| 443 | HTTPS — Synology reverse proxy (veřejný) |
| 8765 | HTTP — aplikace (pouze lokálně na NASu) |
| 8000 | HTTP — uvicorn uvnitř containeru |

---

## Část 1 — Předpoklady

### 1.1 Co musíš mít připraveno

- [ ] Synology NAS s **DSM 7.2** nebo novějším
- [ ] **Container Manager** nainstalovaný z Package Center
- [ ] **Portainer** (volitelné, pro správu přes GUI — doporučeno)
- [ ] **SSH přístup** na NAS povolen (DSM → Control Panel → Terminal & SNMP)
- [ ] Veřejná IP adresa (máš ji — radekn.com)
- [ ] Přístup do DNS registrátoru své domény radekn.com
- [ ] GitHub účet s přístupem k `R4-D3K/gc_flow_analyzer`

### 1.2 Instalace Container Manager

1. DSM → **Package Center** → hledat `Container Manager`
2. Kliknout **Install**
3. Po instalaci otevřít Container Manager a ověřit, že běží

### 1.3 Povolení SSH

1. DSM → **Control Panel → Terminal & SNMP**
2. Zaškrtnout **Enable SSH service**, nastav port (výchozí `22`, lze změnit na jiný)
3. Kliknout **Apply**

> **Tip:** Pro přístup z Windows doporučuji použít **Windows Terminal** nebo **PuTTY**.
> Připojení: `ssh -p <port> admin@<IP-NASu>`
>
> Pokud používáš nestandardní port (např. `73`), přidávej vždy `-p <port>` ke každému `ssh` i `scp` příkazu.

---

## Část 2 — DNS a SSL certifikát

### 2.1 DNS záznam pro novou subdoménu

U svého DNS registrátora přidej **A záznam**:

```
Typ:    A
Název:  gc
Hodnota: <tvoje veřejná IP adresa>
TTL:    3600 (nebo Auto)
```

Výsledek: `gc.radekn.com` → tvoje veřejná IP

> **Ověření propagace DNS** (po 5–30 minutách):
> ```
> nslookup gc.radekn.com
> ```
> Musí vrátit tvoji veřejnou IP.

### 2.2 SSL certifikát (Let's Encrypt)

1. DSM → **Control Panel → Security → Certificate**
2. Kliknout **Add → Add a new certificate**
3. Zvolit **Get a certificate from Let's Encrypt**
4. Vyplnit:
   - Domain name: `gc.radekn.com`
   - Email: tvůj email
5. Kliknout **Done**

> **Pozor:** DNS záznam musí být propagovaný (krok 2.1) než požádáš o certifikát.

---

## Část 3 — Příprava adresářů na NASu

Přihlás se přes SSH:

```bash
ssh admin@radekn.com
```

### 3.1 Vytvoření adresářové struktury

```bash
sudo mkdir -p /volume1/docker/gc-flow-analyzer/data
sudo chown -R $(whoami):users /volume1/docker/gc-flow-analyzer
chmod 750 /volume1/docker/gc-flow-analyzer
chmod 755 /volume1/docker/gc-flow-analyzer/data
```

> **Poznámka:** Adresář `data/` musí mít `chmod 755` (ne 700), jinak ho Docker container
> spuštěný jako non-root user `app` nemůže číst a aplikace selže s `PermissionError`.

### 3.2 Klonování repozitáře

```bash
cd /volume1/docker/gc-flow-analyzer

# Nainstaluj git pokud není dostupný (Synology ho má v Package Center jako Git Server)
git clone https://github.com/R4-D3K/gc_flow_analyzer.git .
```

> Pokud nemáš git, stáhni archiv z GitHubu a rozbal:
> ```bash
> curl -L https://github.com/R4-D3K/gc_flow_analyzer/archive/refs/heads/main.tar.gz -o app.tar.gz
> tar xzf app.tar.gz --strip-components=1
> rm app.tar.gz
> ```

Ověř obsah:

```bash
ls -la /volume1/docker/gc-flow-analyzer/
# Mělo by obsahovat: Dockerfile, docker-compose.yml, manage_orgs.py, app/, ...
```

---

## Část 4 — Generování klíčů a hesel

Tato část se provádí **na tvém PC** (kde máš Python a nainstalované závislosti aplikace), nebo na NASu pokud má Python 3.11+.

### 4.1 Zjisti verzi Pythonu na NASu

```bash
python3 --version
```

Pokud je nižší než 3.10, proveď kroky 4.2–4.4 na svém Windows PC v adresáři projektu.

### 4.2 Generování Fernet šifrovacího klíče

```bash
# Na PC (v adresáři projektu):
python manage_orgs.py generate-key
```

Výstup bude vypadat takto:
```
FC_ENCRYPTION_KEY=9GZ0OWFoU6LC28QlvMWPnmzv3NPfjxzOu0P_ITFz3gs=

Add this to .env.prod — keep it safe, losing it means re-entering all credentials.
```

**Zkopíruj celý řádek `FC_ENCRYPTION_KEY=...` — budeš ho potřebovat v kroku 4.5.**

> ⚠️ **Tento klíč nikdy necommituj do gitu. Nikdy ho nesdílej.
> Pokud ho ztratíš, budeš muset znovu zadat credentials všech orgů.**

### 4.3 Generování SESSION_SECRET

```bash
python -c "import secrets; print('SESSION_SECRET=' + secrets.token_hex(32))"
```

Výstup:
```
SESSION_SECRET=a3f8c2d19e4b76a1f0c5e8d2b9a3f7c1d4e6b8a2f5c9e1d3b7a4f6c2e8d5b1a9
```

**Zkopíruj celý řádek.**

### 4.4 Generování bcrypt hash pro přístupové heslo

```bash
python manage_orgs.py hash-password
```

Budeš vyzván k zadání hesla dvakrát. Zvol silné heslo (doporučuji 12+ znaků, kombinace).

Výstup:
```
APP_PASSWORD_HASH=JDJiJDEyJHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA==
```

**Zkopíruj celý řádek.**

> **Poznámka:** Hash je uložen jako base64 string (bez znaků `$`). Důvod: Docker Compose v2
> interpretuje `$` v env souborech jako proměnné prostředí, což by způsobilo oříznutí bcrypt
> hashe a nefunkční přihlášení. Base64 enkódování tento problém zcela eliminuje.

### 4.5 Vytvoření .env.prod na NASu

Přesuň se zpět do SSH session na NASu:

```bash
cd /volume1/docker/gc-flow-analyzer
cp .env.prod.example .env.prod
nano .env.prod
```

Vyplň soubor hodnotami z kroků 4.2–4.4:

```env
# ── Session & Auth ──────────────────────────────────────────────────
SESSION_SECRET=a3f8c2d19e4b76a1f0c5e8d2b9a3f7c1d4e6b8a2f5c9e1d3b7a4f6c2e8d5b1a9

# Hash vygenerovaný příkazem: python manage_orgs.py hash-password
# Hodnota je base64-enkódovaný bcrypt hash (bez znaků $)
APP_PASSWORD_HASH=JDJiJDEyJHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA==

# ── Org credential encryption ──────────────────────────────────────
FC_ENCRYPTION_KEY=9GZ0OWFoU6LC28QlvMWPnmzv3NPfjxzOu0P_ITFz3gs=

# ── App settings ───────────────────────────────────────────────────
APP_HOST=0.0.0.0
APP_PORT=8000
APP_DEBUG=false

# Session cookie — false = funguje přes HTTP (lokální přístup)
#                  true  = vyžaduje HTTPS (nastavit po konfiguraci reverse proxy)
SESSION_HTTPS_ONLY=false

# Ponech prázdné — aplikace poběží na subdoméně gc.radekn.com
APP_ROOT_PATH=
```

Uložení v nano: `Ctrl+O`, Enter, `Ctrl+X`

**Zabezpeč soubor — nesmí být čitelný pro ostatní uživatele:**

```bash
chmod 600 /volume1/docker/gc-flow-analyzer/.env.prod
```

Ověření:
```bash
ls -la .env.prod
# Musí zobrazit: -rw------- (jen vlastník)
```

---

## Část 5 — Přidání org profilů

Org profily přidáváš přes `manage_orgs.py` — na PC nebo na NASu.
Skript automaticky načte `FC_ENCRYPTION_KEY` ze souboru `.env.prod`.

### 5.1 Přidání prvního orgu

```bash
# Na PC (v adresáři projektu):
python manage_orgs.py add \
  --name "Customer A - EMEA Dublin" \
  --environment mypurecloud.ie \
  --client-id "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" \
  --client-secret "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

Výstup:
```
✓ Org 'Customer A - EMEA Dublin' (mypurecloud.ie) saved to data/orgs.yaml
```

### 5.2 Přidání dalších orgů

Opakuj pro každý org. Příklady podle regionů:

```bash
# US East
python manage_orgs.py add \
  --name "Customer B - Americas" \
  --environment mypurecloud.com \
  --client-id "..." --client-secret "..."

# Frankfurt
python manage_orgs.py add \
  --name "Customer C - EMEA Frankfurt" \
  --environment mypurecloud.de \
  --client-id "..." --client-secret "..."

# London
python manage_orgs.py add \
  --name "Customer D - EMEA London" \
  --environment euw2.pure.cloud \
  --client-id "..." --client-secret "..."
```

### 5.3 Dostupné GC regiony (domain hodnoty pro --environment)

| Region | --environment |
|--------|--------------|
| Americas (US East) | `mypurecloud.com` |
| Americas (US West) | `usw2.pure.cloud` |
| Americas (Canada) | `cac1.pure.cloud` |
| Americas (Mexico) | `mxc1.pure.cloud` |
| South America (São Paulo) | `sae1.pure.cloud` |
| Americas (US East 2, FedRAMP) | `use2.us-gov-pure.cloud` |
| EMEA (Dublin) | `mypurecloud.ie` |
| EMEA (Frankfurt) | `mypurecloud.de` |
| EMEA (London) | `euw2.pure.cloud` |
| EMEA (Zurich) | `euc2.pure.cloud` |
| Asia Pacific (Tokyo) | `mypurecloud.jp` |
| Asia Pacific (Sydney) | `mypurecloud.com.au` |
| Asia Pacific (Seoul) | `apne2.pure.cloud` |
| Asia Pacific (Osaka) | `apne3.pure.cloud` |
| Asia Pacific (Mumbai) | `aps1.pure.cloud` |
| Middle East (UAE) | `mec1.pure.cloud` |

### 5.4 Ověření orgů

```bash
python manage_orgs.py list
```

Výstup (credentials se nezobrazují):
```
#    Name                                     Environment
----------------------------------------------------------------------
1    Customer A - EMEA Dublin                 mypurecloud.ie
2    Customer B - Americas                    mypurecloud.com
3    Customer C - EMEA Frankfurt              mypurecloud.de
```

### 5.5 Zkopírování orgs.yaml na NAS

Pokud jsi přidával orgy na PC, zkopíruj soubor na NAS:

```powershell
# Z Windows PowerShell — uprav port podle svého SSH nastavení:
scp -P <ssh-port> data\orgs.yaml radekn@192.168.1.73:/volume1/docker/gc-flow-analyzer/data/orgs.yaml
```

> **Pokud `scp` selže s `subsystem request failed`** (SFTP subsystém není povolen),
> použij SSH pipe jako náhradu:
> ```powershell
> type data\orgs.yaml | ssh -p <ssh-port> radekn@192.168.1.73 "cat > /volume1/docker/gc-flow-analyzer/data/orgs.yaml"
> ```

Nastav správná oprávnění:
```bash
# SSH na NASu:
chmod 644 /volume1/docker/gc-flow-analyzer/data/orgs.yaml
```

> Soubor obsahuje Fernet-šifrované credentials — obsah bez `FC_ENCRYPTION_KEY` je nečitelný,
> proto `chmod 644` (čitelné pro container) je bezpečné.

---

## Část 6 — Sestavení Docker image

### 6.1 Build image na NASu

```bash
cd /volume1/docker/gc-flow-analyzer
sudo docker build -t gc-flow-analyzer:latest .
```

Build trvá 2–5 minut (stahuje Python dependencies). Úspěšný build zakončí:
```
Successfully tagged gc-flow-analyzer:latest
```

Ověř že image existuje:
```bash
sudo docker images | grep gc-flow-analyzer
# gc-flow-analyzer   latest   abc123def456   2 minutes ago   280MB
```

---

## Část 7 — Spuštění containeru

### Varianta A: docker-compose (doporučeno pro správu přes SSH)

```bash
cd /volume1/docker/gc-flow-analyzer
sudo docker compose up -d
```

Ověř stav:
```bash
sudo docker compose ps
# gc-flow-analyzer   running (healthy)   0.0.0.0:8765->8000/tcp
```

Zobraz logy:
```bash
sudo docker compose logs -f
```

### Varianta B: Portainer Stack (GUI)

1. Otevři Portainer (typicky `http://<IP-NASu>:9000`)
2. Přejdi na **Stacks → Add stack**
3. Název: `gc-flow-analyzer`
4. V sekci **Web editor** vlož obsah souboru `docker-compose.yml`
5. Kliknout **Deploy the stack**
6. Stack se zobrazí v seznamu — container musí mít status **Running**

### 7.1 Ověření funkčnosti containeru (lokálně)

```bash
curl -s http://localhost:8765/health
# {"status":"ok"}
```

---

## Část 8 — Konfigurace Synology Reverse Proxy

### 8.1 Vytvoření reverse proxy pravidla

1. DSM → **Control Panel → Login Portal → Advanced → Reverse Proxy**
2. Kliknout **Create**
3. Vyplnit:

**Source (příchozí požadavky):**
| Pole | Hodnota |
|------|---------|
| Protocol | HTTPS |
| Hostname | `gc.radekn.com` |
| Port | `443` |
| Enable HSTS | ✓ (zaškrtnout) |

**Destination (kam přeposílat):**
| Pole | Hodnota |
|------|---------|
| Protocol | HTTP |
| Hostname | `localhost` |
| Port | `8765` |

4. Záložka **Custom Header** → kliknout **Create → WebSocket**
   (přidá hlavičky `Upgrade` a `Connection` potřebné pro správné fungování)

5. Kliknout **Save**

### 8.2 Přiřazení SSL certifikátu

1. DSM → **Control Panel → Security → Certificate**
2. Kliknout na certifikát `gc.radekn.com`
3. Kliknout **Action → Edit**
4. V sekci **Services** přiřadit certifikát pro `gc.radekn.com` (reverse proxy pravidlo)
5. Kliknout **Confirm**

### 8.3 Aktivace HTTPS-only session cookie

Po zprovoznění HTTPS nastav session cookie na HTTPS-only režim:

```bash
# SSH na NASu:
nano /volume1/docker/gc-flow-analyzer/.env.prod
# Změň: SESSION_HTTPS_ONLY=false  →  SESSION_HTTPS_ONLY=true
# Uložit: Ctrl+O, Enter, Ctrl+X

sudo docker compose restart
```

### 8.4 Ověření reverse proxy

```bash
# Z PC nebo z mobilu mimo lokální síť:
curl -s https://gc.radekn.com/health
# {"status":"ok"}
```

Pokud vidíš `{"status":"ok"}`, aplikace je dostupná z internetu.

---

## Část 9 — Ověření nasazení

### 9.1 Test přihlášení

Otevři prohlížeč a přejdi na:

```
https://gc.radekn.com/
```

Měla by se zobrazit **přihlašovací stránka** aplikace. Zadej heslo, které jsi nastavil v kroku 4.4.

### 9.2 Test analýzy

Po přihlášení:
1. V dropdownu vyber org profil
2. Vlož platné Conversation ID z Genesys Cloud
3. Klikni **Analyze**
4. Zkontroluj že se zobrazí výsledky se záložkami Steps / Variable Changes / Flow Diagram

### 9.3 Kontrolní seznam nasazení

- [ ] `https://gc.radekn.com/health` vrací `{"status":"ok"}`
- [ ] `https://gc.radekn.com/` zobrazí přihlašovací stránku
- [ ] Přihlášení heslem funguje
- [ ] Org dropdown zobrazuje správné orgy
- [ ] Analýza konkrétního Conversation ID funguje
- [ ] Certifikát je platný (zelený zámek v prohlížeči)
- [ ] Přihlášení přetrvá po refreshi stránky (session cookie funguje)

---

## Část 10 — Průběžná správa

### 10.1 Přidání nového org profilu

```bash
# Na PC v adresáři projektu:
python manage_orgs.py add \
  --name "New Customer - Region" \
  --environment euc2.pure.cloud \
  --client-id "..." --client-secret "..."

# Zkopíruj aktualizovaný orgs.yaml na NAS (uprav port):
scp -P <ssh-port> data\orgs.yaml radekn@192.168.1.73:/volume1/docker/gc-flow-analyzer/data/orgs.yaml

# Restart containeru (načtení nového orgs.yaml):
ssh -p <ssh-port> radekn@192.168.1.73 "cd /volume1/docker/gc-flow-analyzer && sudo docker compose restart"
```

### 10.2 Smazání org profilu

```bash
python manage_orgs.py delete --name "Old Customer"
scp -P <ssh-port> data\orgs.yaml radekn@192.168.1.73:/volume1/docker/gc-flow-analyzer/data/orgs.yaml
ssh -p <ssh-port> radekn@192.168.1.73 "cd /volume1/docker/gc-flow-analyzer && sudo docker compose restart"
```

### 10.3 Aktualizace aplikace na novou verzi

```bash
# SSH na NASu:
cd /volume1/docker/gc-flow-analyzer

# Stáhni nový kód:
git pull origin main

# Znovu postav image:
sudo docker compose build

# Restartuj container (zero-downtime není potřeba pro osobní nástroj):
sudo docker compose up -d --force-recreate
```

### 10.4 Zobrazení logů

```bash
# SSH na NASu:
cd /volume1/docker/gc-flow-analyzer

# Poslední logy:
sudo docker compose logs --tail=100

# Průběžné sledování:
sudo docker compose logs -f

# Logy jen aplikace (filtr na WARNING a výše):
sudo docker compose logs -f | grep -E "WARNING|ERROR|CRITICAL"
```

### 10.5 Restart / zastavení / spuštění

```bash
sudo docker compose restart    # restart (zachová konfiguraci)
sudo docker compose stop       # zastavení
sudo docker compose start      # spuštění
sudo docker compose down       # zastavení + smazání containeru (image zůstane)
```

### 10.6 Změna přístupového hesla

```bash
# Na PC:
python manage_orgs.py hash-password
# → zkopíruj nový APP_PASSWORD_HASH

# Na NASu:
nano /volume1/docker/gc-flow-analyzer/.env.prod
# Aktualizuj řádek APP_PASSWORD_HASH=...
# Uložit: Ctrl+O, Enter, Ctrl+X

sudo docker compose restart
```

### 10.7 Záloha

Důležité soubory k zálohování:

| Soubor | Obsah | Priorita |
|--------|-------|----------|
| `/volume1/docker/gc-flow-analyzer/.env.prod` | SESSION_SECRET, APP_PASSWORD_HASH, **FC_ENCRYPTION_KEY** | ⚠️ Kritická |
| `/volume1/docker/gc-flow-analyzer/data/orgs.yaml` | Šifrované org credentials | ⚠️ Kritická |

> **Bez `FC_ENCRYPTION_KEY` nelze dešifrovat `orgs.yaml`.**
> Ztráta klíče = nutnost znovu zadat credentials všech orgů.

```bash
# Záloha na bezpečné místo (např. Synology Drive nebo USB):
cp /volume1/docker/gc-flow-analyzer/.env.prod /volume1/backup/gcanalyzer/.env.prod.backup
cp /volume1/docker/gc-flow-analyzer/data/orgs.yaml /volume1/backup/gcanalyzer/orgs.yaml.backup
```

---

## Část 11 — Řešení problémů (Troubleshooting)

### Aplikace nereaguje na `https://gc.radekn.com/`

1. Ověř DNS: `nslookup gc.radekn.com` musí vrátit tvoji IP
2. Ověř container: `sudo docker compose ps` — musí být `running`
3. Ověř lokálně: `curl http://localhost:8765/health`
4. Ověř reverse proxy pravidlo v DSM
5. Ověř firewall na routeru — port 443 musí být otevřen

### Aplikace vrací `502 Bad Gateway`

Container neběží nebo nenaslouchá na portu 8765.

```bash
sudo docker compose ps
sudo docker compose logs --tail=50
```

Typické příčiny:
- Chybný `APP_PASSWORD_HASH` (prázdný nebo neplatný bcrypt hash)
- Chybný `FC_ENCRYPTION_KEY` (neplatný Fernet klíč)
- Špatná syntaxe v `.env.prod`

### Přihlášení nefunguje — "Invalid password"

```bash
# Ověř co container skutečně vidí jako APP_PASSWORD_HASH:
sudo docker exec gc-flow-analyzer env | grep APP_PASSWORD_HASH
# Musí být base64 string (dlouhý řetězec bez znaků $)
# Pokud je prázdný nebo zkrácený → problém s $ interpolací (viz níže)
```

> **Problém s `$` interpolací v docker-compose:**
> Docker Compose v2 interpretuje `$` v env souborech jako proměnné prostředí.
> Proto `APP_PASSWORD_HASH` **musí být vygenerován příkazem `python manage_orgs.py hash-password`**
> který automaticky base64-enkóduje hash a odstraní všechny `$` znaky.
> Nikdy nekopíruj raw bcrypt hash (začínající `$2b$12$...`) přímo do `.env.prod`.

### Přihlášení selže — po zadání hesla se znovu zobrazí login

```bash
# Ověř nastavení session cookie:
grep SESSION_HTTPS_ONLY /volume1/docker/gc-flow-analyzer/.env.prod
```

- Přistupuješ přes **HTTP** (lokálně): musí být `SESSION_HTTPS_ONLY=false`
- Přistupuješ přes **HTTPS** (reverse proxy): musí být `SESSION_HTTPS_ONLY=true`

```bash
# Ověř SESSION_SECRET:
grep SESSION_SECRET /volume1/docker/gc-flow-analyzer/.env.prod
# Nesmí být prázdný nebo "change-me"
```

### Org profily se nezobrazují (prázdný dropdown)

```bash
# Ověř že orgs.yaml existuje:
ls -la /volume1/docker/gc-flow-analyzer/data/

# Ověř logy při startu:
sudo docker compose logs | grep -E "orgs|Loaded|ENCRYPTION"

# Ověř že FC_ENCRYPTION_KEY v .env.prod odpovídá klíči použitému při šifrování orgů
```

### Analýza vrací "Authentication failed"

- OAuth Client ID nebo Secret je neplatný
- Client nemá požadovaná oprávnění v GC org
- Org byl deaktivován

**Požadovaná oprávnění GC OAuth clienta:**
- `Analytics > Conversation Detail > View`
- `Architect > Flow > View`
- `Architect > Flow Execution > View`
- `Architect > flowInstance > All Permissions`
- `Architect > flowInstanceExecutionData > All Permissions`

### Session se nepersistuje (neustálé odhlašování přes HTTPS)

Pravděpodobná příčina: `SESSION_HTTPS_ONLY=true` ale HTTPS není správně ukončeno na reverse proxy.

Ověř:
1. Certifikát je platný a přiřazený k `gc.radekn.com`
2. Prohlížeč přistupuje přes `https://` (ne `http://`)
3. V `.env.prod` je `SESSION_HTTPS_ONLY=true`
4. Reverse proxy předává hlavičku `X-Forwarded-Proto: https`

### SSL certifikát nelze vygenerovat (Let's Encrypt error)

- DNS musí propagovat před žádostí o certifikát (počkej 15–30 minut)
- Port 80 musí být dostupný z internetu (Let's Encrypt HTTP challenge)
- Synology musí mít přístup k internetu

---

## Část 12 — Bezpečnostní přehled

| Vrstva | Mechanismus | Poznámka |
|--------|-------------|----------|
| Transport | TLS 1.2/1.3 (Let's Encrypt) | Automatická obnova v DSM |
| Přístup k aplikaci | bcrypt heslo (base64) + session cookie | Session 8h, HTTPS-only při `SESSION_HTTPS_ONLY=true` |
| Credentials v klidovém stavu | Fernet (AES-128-CBC + HMAC-SHA256) | Klíč pouze v env var |
| Docker image | Non-root user (`app`) | Omezená práva uvnitř containeru |
| orgs.yaml | chmod 644, read-only mount, Fernet šifrování | Container nemůže soubor přepsat, obsah je šifrovaný |
| .env.prod | chmod 600 | Čitelné jen pro admin |

---

## Příloha A — Struktura souborů na NASu

```
/volume1/docker/gc-flow-analyzer/
├── .env.prod                  ← production konfigurace (chmod 600)
├── Dockerfile
├── docker-compose.yml
├── manage_orgs.py             ← CLI správa orgů
├── requirements.txt
├── app/
│   ├── main.py
│   ├── config.py
│   ├── auth.py
│   ├── orgs.py
│   ├── gc_client.py
│   ├── flow_parser.py
│   └── templates/
└── data/
    └── orgs.yaml              ← šifrované org credentials (chmod 644)
```

---

## Příloha B — Rychlý přehled příkazů

```bash
# Stav containeru
sudo docker compose ps

# Logy
sudo docker compose logs -f

# Restart
sudo docker compose restart

# Aktualizace
git pull && sudo docker compose build && sudo docker compose up -d --force-recreate

# Přidání orgu (na PC)
python manage_orgs.py add --name "..." --environment "..." --client-id "..." --client-secret "..."

# Výpis orgů
python manage_orgs.py list

# Smazání orgu
python manage_orgs.py delete --name "..."

# Nové heslo
python manage_orgs.py hash-password
```
