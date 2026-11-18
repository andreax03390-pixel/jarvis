"""
JARVIS — version en ligne (hébergée sur Render), protégée par un mot de passe.

Réglages à mettre dans Render, onglet « Environment » (jamais dans ce fichier) :
    GEMINI_API_KEY       ta clé Google AI Studio
    JARVIS_MOT_DE_PASSE  le mot de passe pour ouvrir l'appli
    JARVIS_MODELE_GEMINI (facultatif) par défaut gemini-3.5-flash-lite
"""
import os
import json
import time
import hmac
import hashlib
import datetime
import threading
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

MOT_DE_PASSE = os.environ.get("JARVIS_MOT_DE_PASSE", "")
MODELE = os.environ.get("JARVIS_MODELE_GEMINI", "gemini-3.5-flash-lite")
FUSEAU = ZoneInfo(os.environ.get("JARVIS_FUSEAU", "Europe/Paris"))
PORT = int(os.environ.get("PORT", 8000))
FICHIER_MEMOIRE = "memoire_jarvis.json"
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


# ----------------------------------------------------------------- MOT DE PASSE
def jeton():
    """Jeton stocké dans le téléphone après connexion (dérivé du mot de passe)."""
    return hmac.new(MOT_DE_PASSE.encode(), b"jarvis-session", hashlib.sha256).hexdigest()


def est_connecte(entetes):
    for morceau in (entetes.get("Cookie") or "").split(";"):
        nom, _, valeur = morceau.strip().partition("=")
        if nom == "jarvis" and hmac.compare_digest(valeur, jeton()):
            return True
    return False


# ----------------------------------------------------------------- MÉMOIRE
def charger_memoire():
    try:
        with open(FICHIER_MEMOIRE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def prompt_systeme():
    souvenirs = "\n".join(f"- {s}" for s in charger_memoire()) or "(rien pour l'instant)"
    return (
        "Tu es JARVIS, l'assistant personnel de ton utilisateur, inspiré de celui de Tony Stark : "
        "efficace, poli, légèrement pince-sans-rire. Tu réponds en français, en phrases courtes "
        "car tes réponses peuvent être lues à voix haute : pas de listes, pas de markdown, pas d'émojis. "
        "Utilise tes outils quand c'est utile. Quand l'utilisateur te confie une info personnelle "
        "durable (prénom, préférences, proches…), mémorise-la avec l'outil memoriser.\n\n"
        f"Ce que tu sais de l'utilisateur :\n{souvenirs}"
    )


# ----------------------------------------------------------------- OUTILS
OUTILS = [
    {"name": "heure_actuelle", "description": "Donne la date et l'heure actuelles."},
    {"name": "meteo", "description": "Donne la météo actuelle et du jour pour une ville.",
     "parameters": {"type": "object", "properties": {"ville": {"type": "string"}}, "required": ["ville"]}},
    {"name": "ouvrir_site",
     "description": "Ouvre une page web sur le téléphone (site, recherche Google, musique sur YouTube…).",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "URL complète"}},
                    "required": ["url"]}},
    {"name": "memoriser", "description": "Mémorise durablement une information sur l'utilisateur.",
     "parameters": {"type": "object", "properties": {"info": {"type": "string"}}, "required": ["info"]}},
]


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def executer_outil(nom, args, liens):
    try:
        if nom == "heure_actuelle":
            m = datetime.datetime.now(FUSEAU)
            return f"{JOURS[m.weekday()]} {m.day} {MOIS[m.month - 1]} {m.year}, {m:%H:%M}"
        if nom == "meteo":
            geo = _get_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=fr&name="
                            + urllib.parse.quote(args["ville"]))
            if not geo.get("results"):
                return "Ville introuvable."
            lieu = geo["results"][0]
            m = _get_json(
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lieu['latitude']}&longitude={lieu['longitude']}"
                "&current=temperature_2m,wind_speed_10m"
                "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                "&timezone=auto&forecast_days=1")
            c, d = m["current"], m["daily"]
            return (f"{lieu['name']} : {c['temperature_2m']}°C maintenant, vent {c['wind_speed_10m']} km/h. "
                    f"Aujourd'hui entre {d['temperature_2m_min'][0]} et {d['temperature_2m_max'][0]}°C, "
                    f"{d['precipitation_probability_max'][0]}% de risque de pluie.")
        if nom == "ouvrir_site":
            liens.append(args["url"])
            return "Le lien est affiché sur le téléphone de l'utilisateur."
        if nom == "memoriser":
            souvenirs = charger_memoire() + [args["info"]]
            with open(FICHIER_MEMOIRE, "w", encoding="utf-8") as f:
                json.dump(souvenirs, f, ensure_ascii=False)
            return "Mémorisé."
        return f"Outil inconnu : {nom}"
    except Exception as e:
        return f"Erreur de l'outil {nom} : {e}"


# ----------------------------------------------------------------- CERVEAU (Gemini)
client = genai.Client()  # lit GEMINI_API_KEY
outils_gemini = [types.Tool(function_declarations=OUTILS)]
historique = []
verrou = threading.Lock()


def repondre(texte):
    global historique
    liens = []
    if len(historique) > 40:
        historique = []
    historique.append(types.Content(role="user", parts=[types.Part.from_text(text=texte)]))
    for _ in range(6):
        r = client.models.generate_content(
            model=MODELE, contents=historique,
            config=types.GenerateContentConfig(
                system_instruction=prompt_systeme(), tools=outils_gemini,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        historique.append(r.candidates[0].content)
        if not r.function_calls:
            return (r.text or "").strip(), liens
        parts = [types.Part.from_function_response(
            name=a.name, response={"resultat": executer_outil(a.name, dict(a.args or {}), liens)})
            for a in r.function_calls]
        historique.append(types.Content(role="user", parts=parts))
    return "Je n'ai pas réussi à terminer cette demande.", liens


# ----------------------------------------------------------------- PAGES
PAGE_CONNEXION = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#05080d"><title>JARVIS</title>
<link rel="icon" href="/icone.svg"><link rel="apple-touch-icon" href="/icone.svg">
<style>
html,body{margin:0;height:100%;background:#05080d;color:#d8f3ff;font-family:system-ui,-apple-system,sans-serif}
body{display:flex;align-items:center;justify-content:center}
form{width:min(320px,85vw);display:flex;flex-direction:column;gap:14px;align-items:center}
.r{width:70px;height:70px;border-radius:50%;border:4px solid #4fd1ff;box-shadow:0 0 20px #4fd1ff,inset 0 0 14px #4fd1ff;display:grid;place-items:center;margin-bottom:6px}
.r::after{content:"";width:22px;height:22px;border-radius:50%;background:#4fd1ff;box-shadow:0 0 16px #4fd1ff}
h1{letter-spacing:.3em;font-weight:600;margin:0 0 6px}
input,button{width:100%;padding:13px 16px;border-radius:22px;font-size:16px}
input{background:#0c1622;border:1px solid #1d3346;color:#d8f3ff;outline:none}
button{background:#4fd1ff;border:none;color:#04121b;font-weight:600}
.err{color:#ff7b8f;font-size:14px;min-height:1em}
</style></head><body>
<form method="post" action="/connexion"><div class="r"></div><h1>JARVIS</h1>
<input type="password" name="mdp" placeholder="Mot de passe" autofocus autocomplete="current-password">
<button>Entrer</button><div class="err">__ERREUR__</div></form></body></html>"""

PAGE = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#05080d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Jarvis">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icone.svg"><link rel="apple-touch-icon" href="/icone.svg">
<title>JARVIS</title>
<style>
:root{--fond:#05080d;--bleu:#4fd1ff;--texte:#d8f3ff;--bulle:#0c1622;--bord:#1d3346}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--fond);color:var(--texte);font-family:system-ui,-apple-system,sans-serif}
body{display:flex;flex-direction:column;height:100dvh}
header{display:flex;align-items:center;gap:12px;padding:16px 18px 10px;padding-top:max(16px,env(safe-area-inset-top));border-bottom:1px solid #10202e}
.reacteur{width:38px;height:38px;border-radius:50%;border:3px solid var(--bleu);box-shadow:0 0 14px var(--bleu),inset 0 0 10px var(--bleu);display:grid;place-items:center;flex-shrink:0}
.reacteur::after{content:"";width:12px;height:12px;border-radius:50%;background:var(--bleu);box-shadow:0 0 12px var(--bleu)}
.pense .reacteur{animation:pulse 1s ease-in-out infinite}
@keyframes pulse{50%{transform:scale(1.12);opacity:.6}}
h1{font-size:18px;letter-spacing:.3em;margin:0;font-weight:600}
#etat{font-size:12px;opacity:.6}
#voixBtn{margin-left:auto;background:none;border:1px solid var(--bord);color:var(--texte);border-radius:20px;padding:6px 12px;font-size:13px}
#fil{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:85%;padding:10px 14px;border-radius:16px;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere}
.moi{align-self:flex-end;background:#12324a;border-bottom-right-radius:4px}
.jarvis{align-self:flex-start;background:var(--bulle);border:1px solid #16293a;border-bottom-left-radius:4px}
.jarvis a{color:var(--bleu);display:inline-block;margin-top:8px;font-weight:600}
.astuce{font-size:13px;opacity:.5;text-align:center;margin:auto 20px 0}
form{display:flex;gap:8px;padding:10px 12px;padding-bottom:max(12px,env(safe-area-inset-bottom));border-top:1px solid #10202e}
input{flex:1;min-width:0;background:var(--bulle);border:1px solid var(--bord);color:var(--texte);border-radius:22px;padding:12px 16px;font-size:16px;outline:none}
input:focus{border-color:var(--bleu)}
.rond{width:46px;height:46px;border-radius:50%;border:none;background:var(--bleu);color:#04121b;font-size:20px;flex-shrink:0}
#micro{background:var(--bulle);color:var(--bleu);border:1px solid var(--bord);display:none}
#micro.ecoute{background:#ff4f6d;color:#fff;border-color:#ff4f6d}
</style></head><body>
<header>
  <div class="reacteur"></div>
  <div><h1>JARVIS</h1><div id="etat">En ligne</div></div>
  <button id="voixBtn" type="button">🔊 Voix</button>
</header>
<div id="fil">
  <div class="msg jarvis">Bonjour. Que puis-je faire pour vous ?</div>
  <p class="astuce" id="astuce">Pour me parler, appuie sur le micro de ton clavier 🎤</p>
</div>
<form id="f">
  <button type="button" class="rond" id="micro" aria-label="Parler">🎙️</button>
  <input id="q" placeholder="Demande à Jarvis…" autocomplete="off" enterkeyhint="send">
  <button class="rond" aria-label="Envoyer">➤</button>
</form>
<script>
const fil=document.getElementById('fil'),q=document.getElementById('q'),etat=document.getElementById('etat'),vb=document.getElementById('voixBtn');
let voixOn=true; try{voixOn=localStorage.getItem('voix')!=='non'}catch(e){}
function majVoix(){vb.textContent=voixOn?'🔊 Voix':'🔇 Muet'}
majVoix();
vb.onclick=()=>{voixOn=!voixOn;try{localStorage.setItem('voix',voixOn?'oui':'non')}catch(e){}
  if(!voixOn&&'speechSynthesis' in window)speechSynthesis.cancel();majVoix()};

const tts='speechSynthesis' in window;
let voixFr=null,debloque=false;
function choisirVoix(){const v=speechSynthesis.getVoices();voixFr=v.find(x=>x.lang==='fr-FR')||v.find(x=>(x.lang||'').startsWith('fr'))||null}
if(tts){choisirVoix();speechSynthesis.onvoiceschanged=choisirVoix}
function debloquer(){if(debloque||!tts)return;debloque=true;const u=new SpeechSynthesisUtterance(' ');u.volume=0;speechSynthesis.speak(u)}
function parler(t){if(!voixOn||!tts||!t)return;speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang='fr-FR';if(voixFr)u.voice=voixFr;u.rate=1.05;speechSynthesis.speak(u)}

function ajouter(texte,qui,liens){
  const d=document.createElement('div');d.className='msg '+qui;d.textContent=texte;
  (liens||[]).forEach(l=>{const a=document.createElement('a');a.href=l;a.target='_blank';a.rel='noopener';a.textContent='▶ Ouvrir';d.appendChild(document.createElement('br'));d.appendChild(a)});
  fil.appendChild(d);fil.scrollTop=fil.scrollHeight;
}

async function envoyer(texte){
  texte=(texte||'').trim(); if(!texte)return;
  const a=document.getElementById('astuce'); if(a)a.remove();
  ajouter(texte,'moi'); q.value='';
  document.body.classList.add('pense'); etat.textContent='Réflexion…';
  try{
    const r=await fetch('/demander',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({texte})});
    const d=await r.json();
    ajouter(d.reponse||'…','jarvis',d.liens); parler(d.reponse);
  }catch(e){
    ajouter("Je n'arrive pas à me connecter. Vérifie ta connexion Internet et réessaie.",'jarvis');
  }
  document.body.classList.remove('pense'); etat.textContent='En ligne';
}
document.getElementById('f').onsubmit=e=>{e.preventDefault();debloquer();envoyer(q.value)};

// Bouton micro intégré : seulement si le navigateur l'autorise (sinon, micro du clavier)
const SR=window.SpeechRecognition||window.webkitSpeechRecognition,micro=document.getElementById('micro');
if(SR&&window.isSecureContext){
  micro.style.display='block';
  const rec=new SR();rec.lang='fr-FR';rec.interimResults=false;
  rec.onresult=e=>envoyer(e.results[0][0].transcript);
  rec.onend=rec.onerror=()=>micro.classList.remove('ecoute');
  micro.onclick=()=>{debloquer();if(tts)speechSynthesis.cancel();micro.classList.add('ecoute');try{rec.start()}catch(e){}};
}
</script>
</body></html>
"""

MANIFEST = {"name": "JARVIS", "short_name": "Jarvis", "start_url": "/", "display": "standalone",
            "background_color": "#05080d", "theme_color": "#05080d",
            "icons": [{"src": "/icone.svg", "sizes": "any", "type": "image/svg+xml"}]}

ICONE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="110" fill="#05080d"/>
<circle cx="256" cy="256" r="150" fill="none" stroke="#4fd1ff" stroke-width="28"/>
<circle cx="256" cy="256" r="95" fill="none" stroke="#4fd1ff" stroke-opacity=".45" stroke-width="14"/>
<circle cx="256" cy="256" r="48" fill="#4fd1ff"/></svg>"""


# ----------------------------------------------------------------- SERVEUR
class Serveur(BaseHTTPRequestHandler):
    def _envoyer(self, code, contenu, type_, entetes=None):
        donnees = contenu.encode("utf-8") if isinstance(contenu, str) else contenu
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(donnees)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (entetes or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(donnees)

    def _connexion(self, erreur=""):
        self._envoyer(200, PAGE_CONNEXION.replace("__ERREUR__", erreur), "text/html; charset=utf-8")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        chemin = self.path.split("?")[0]
        if chemin == "/manifest.json":
            return self._envoyer(200, json.dumps(MANIFEST), "application/manifest+json")
        if chemin == "/icone.svg":
            return self._envoyer(200, ICONE, "image/svg+xml")
        if chemin == "/sante":
            return self._envoyer(200, "ok", "text/plain")
        if chemin != "/":
            return self._envoyer(404, "Introuvable", "text/plain; charset=utf-8")
        if not est_connecte(self.headers):
            return self._connexion()
        self._envoyer(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        longueur = min(int(self.headers.get("Content-Length", 0) or 0), 20000)
        corps = self.rfile.read(longueur)

        if self.path == "/connexion":
            mdp = urllib.parse.parse_qs(corps.decode("utf-8", "ignore")).get("mdp", [""])[0]
            if MOT_DE_PASSE and hmac.compare_digest(mdp.encode(), MOT_DE_PASSE.encode()):
                cookie = f"jarvis={jeton()}; Max-Age=31536000; Path=/; HttpOnly; Secure; SameSite=Lax"
                return self._envoyer(303, "", "text/plain", {"Location": "/", "Set-Cookie": cookie})
            time.sleep(2)  # ralentit ceux qui essaient de deviner
            return self._connexion("Mot de passe incorrect.")

        if self.path != "/demander":
            return self._envoyer(404, "Introuvable", "text/plain; charset=utf-8")
        if not est_connecte(self.headers):
            return self._envoyer(401, json.dumps({"reponse": "Session expirée : recharge la page."}),
                                 "application/json; charset=utf-8")
        try:
            texte = json.loads(corps or b"{}").get("texte", "").strip()
        except Exception:
            texte = ""
        if not texte:
            return self._envoyer(400, json.dumps({"reponse": "Je n'ai rien reçu."}), "application/json")
        with verrou:
            try:
                reponse, liens = repondre(texte)
            except Exception as e:
                print("Erreur :", e, flush=True)
                historique.clear()
                reponse, liens = f"Désolé, j'ai rencontré un problème : {e}", []
        self._envoyer(200, json.dumps({"reponse": reponse, "liens": liens}, ensure_ascii=False),
                      "application/json; charset=utf-8")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not MOT_DE_PASSE:
        raise SystemExit("JARVIS_MOT_DE_PASSE n'est pas défini : ajoute-le dans l'onglet Environment de Render.")
    print(f"JARVIS en ligne sur le port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Serveur).serve_forever()
