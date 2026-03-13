# LSPD Discord Bot – rychlé nasazení 24/7

## 1) Co vyplnit
V souboru `.env` vyplň:
- `DISCORD_TOKEN`
- `GUILD_ID`
- `LOG_CHANNEL_ID`
- `AUTO_EYE_CHANNEL_IDS` (čárkou oddělené ID kanálů)

## 2) Lokální spuštění (test)
```bash
pip install -r requirements.txt
python main.py
```

## 3) Render (doporučeno)
1. Nahraj složku do GitHub repozitáře.
2. Na Renderu klikni **New +** -> **Blueprint** a vyber repo.
3. Render načte `render.yaml` a vytvoří worker službu.
4. V Render dashboardu doplň env proměnné z `.env`.
5. Deploy a bot poběží 24/7.

## 4) Railway (alternativa)
1. V Railway klikni **New Project** -> **Deploy from GitHub repo**.
2. Railway použije `railway.toml` a start command `python main.py`.
3. Doplň env proměnné v sekci Variables.
4. Deploy.

## 5) Discord Developer Portal
Bot musí mít v Portalu zapnuté:
- `MESSAGE CONTENT INTENT`
- `SERVER MEMBERS INTENT`

A při pozvání bota dej scope:
- `bot`
- `applications.commands`

Doporučená oprávnění: Manage Messages, Kick Members, Ban Members, Moderate Members, Read/Send Messages.
