import discord
from discord.ext import commands
from discord import app_commands, Embed
from discord.ui import View, Button
from discord import ButtonStyle
import asyncio
import yt_dlp
import re
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from spotify_scraper import SpotifyClient # <-- CORRECTED HERE
from spotify_scraper.core.exceptions import SpotifyScraperError
import random
from urllib.parse import urlparse, parse_qs
from cachetools import TTLCache
import logging
import requests
from playwright.async_api import async_playwright
import json
import math

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Intents for the bot
intents = discord.Intents.all()
intents.guilds = True
intents.voice_states = True

# Create the bot
bot = commands.Bot(command_prefix="!", intents=intents)

# Client for the Official API (fast and priority)
SPOTIFY_CLIENT_ID = 'CLIENTIDHERE' # Your ID
SPOTIFY_CLIENT_SECRET = 'CLIENTSECRETHERE' # Your Secret
try:
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET
    ))
    logger.info("Spotipy API client initialized successfully.")
except Exception as e:
    sp = None
    logger.error(f"Unable to initialize the Spotipy client : {e}")

# Client for the Scraper (plan B, with Selenium)
try:
    # We use the correct class name, without aliases
    spotify_scraper_client = SpotifyClient(browser_type="selenium") # <-- CORRECTED HERE
    logger.info("SpotifyScraper client successfully initialized in Selenium mode.")
except Exception as e:
    spotify_scraper_client = None
    logger.error(f"Unable to initialize SpotifyScraper : {e}")

# Cache for YouTube searches (2-hour TTL, size for 500+ servers)
url_cache = TTLCache(maxsize=75000, ttl=7200)

# Normalize strings for search queries
def sanitize_query(query):
    query = re.sub(r'[\x00-\x1F\x7F]', '', query)  # Remove control chars
    query = re.sub(r'\s+', ' ', query).strip()  # Normalize spaces
    return query

# Retry Spotify API calls with backoff
async def retry_spotify_call(func, *args, max_retries=3, backoff_factor=1.5, **kwargs):
    for attempt in range(max_retries):
        try:
            return await asyncio.get_event_loop().run_in_executor(None, lambda: func(*args, **kwargs))
        except spotipy.exceptions.SpotifyException as se:
            if se.http_status in [400, 401, 404, 429]:
                if attempt == max_retries - 1:
                    raise
                wait_time = backoff_factor * (2 ** attempt)
                logger.warning(f"Spotify API error {se.http_status}: {se.msg}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                raise
        except Exception as e:
            logger.error(f"Unexpected error in Spotify call: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(backoff_factor)
    raise Exception("Max retries reached for Spotify API call")

# Async yt-dlp info extraction
async def extract_info_async(ydl_opts, query, loop=None):
    if loop is None:
        loop = asyncio.get_running_loop()
    
    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(query, download=False)
    
    return await loop.run_in_executor(None, extract)

# Create loading bar
def create_loading_bar(progress, width=10):
    filled = int(progress * width)
    unfilled = width - filled
    return '```[' + '█' * filled + '░' * unfilled + '] ' + f'{int(progress * 100)}%```'

# Music player state
class MusicPlayer:
    def __init__(self):
        self.voice_client = None
        self.current_task = None
        self.queue = asyncio.Queue()
        self.current_url = None
        self.current_info = None
        self.text_channel = None
        self.loop_current = False
        self.autoplay_enabled = False
        self.last_was_single = False

# Server states
music_players = {}  # {guild_id: MusicPlayer()}
kawaii_mode = {}    # {guild_id: bool}
server_languages = {}  # {guild_id: "en" or "fr"}

# Get player for a server
def get_player(guild_id):
    if guild_id not in music_players:
        music_players[guild_id] = MusicPlayer()
    return music_players[guild_id]

# Get kawaii mode
def get_mode(guild_id):
    return kawaii_mode.get(guild_id, False)

# Get language
def get_language(guild_id):
    return server_languages.get(guild_id, "en")

# Messages based on language and kawaii mode
def get_messages(message_key, guild_id):
    is_kawaii = get_mode(guild_id)
    lang = get_language(guild_id)
    
    messages = {
        "en": {
            "no_voice_channel": {
                "normal": "You must be in a voice channel to use this command.",
                "kawaii": "(>ω<) You must be in a voice channel!"
            },
            "connection_error": {
                "normal": "Error connecting to the voice channel.",
                "kawaii": "(╥﹏╥) I couldn't connect..."
            },
            "spotify_error": {
                "normal": "Error processing the Spotify link. It may be private, region-locked, or invalid.",
                "kawaii": "(´；ω；`) Oh no! Problem with the Spotify link... maybe it’s shy or hidden?"
            },
            "spotify_playlist_added": {
                "normal": "🎶 Spotify Playlist Added",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ SPOTIFY PLAYLIST"
            },
            "spotify_playlist_description": {
                "normal": "**{count} tracks** added, {failed} failed.\n{failed_tracks}",
                "kawaii": "**{count} songs** added, {failed} couldn’t join! (´･ω･`)\n{failed_tracks}"
            },
            "deezer_error": {
                "normal": "Error processing the Deezer link. It may be private, region-locked, or invalid.",
                "kawaii": "(´；ω；`) Oh no! Problem with the Deezer link... maybe it’s shy or hidden?"
            },
            "deezer_playlist_added": {
                "normal": "🎶 Deezer Playlist Added",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ DEEZER PLAYLIST"
            },
            "deezer_playlist_description": {
                "normal": "**{count} tracks** added, {failed} failed.\n{failed_tracks}",
                "kawaii": "**{count} songs** added, {failed} couldn’t join! (´･ω･`)\n{failed_tracks}"
            },
            "apple_music_error": {
                "normal": "Error processing the Apple Music link.",
                "kawaii": "(´；ω；`) Oops! Trouble with the Apple Music link..."
            },
            "apple_music_playlist_added": {
                "normal": "🎶 Apple Music Playlist Added",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ APPLE MUSIC PLAYLIST"
            },
            "apple_music_playlist_description": {
                "normal": "**{count} tracks** added, {failed} failed.\n{failed_tracks}",
                "kawaii": "**{count} songs** added, {failed} couldn't join! (´･ω･`)\n{failed_tracks}"
            },
            "tidal_error": {
                "normal": "Error processing the Tidal link. It may be private, region-locked, or invalid.",
                "kawaii": "(´；ω；`) Oh no! Problem with the Tidal link... maybe it’s shy or hidden?"
            },
            "tidal_playlist_added": {
                "normal": "🎶 Tidal Playlist Added",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ TIDAL PLAYLIST"
            },
            "tidal_playlist_description": {
                "normal": "**{count} tracks** added, {failed} failed.\n{failed_tracks}",
                "kawaii": "**{count} songs** added, {failed} couldn’t join! (´･ω･`)\n{failed_tracks}"
            },
            "amazon_music_error": {
                "normal": "Error processing the Amazon Music link.",
                "kawaii": "(´；ω；`) Oh no! Something is wrong with the Amazon Music link..." 
            },
            "amazon_music_playlist_added": {
                 "normal": "🎶 Amazon Music Playlist Added", 
                 "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ AMAZON MUSIC PLAYLIST" 
            },
            "amazon_music_playlist_description": {
                 "normal": "**{count} tracks** added, {failed} failed.\n{failed_tracks}", 
                 "kawaii": "**{count} songs** added, {failed} couldn't join! (´･ω･`)\n{failed_tracks}" 
            },
            "song_added": {
                "normal": "🎵 Added to Queue",
                "kawaii": "(っ◕‿◕)っ ♫ SONG ADDED ♫"
            },
            "playlist_added": {
                "normal": "🎶 Playlist Added",
                "kawaii": "✧･ﾟ: *✧･ﾟ:* PLAYLIST *:･ﾟ✧*:･ﾟ✧"
            },
            "playlist_description": {
                "normal": "**{count} tracks** added to the queue.",
                "kawaii": "**{count} songs** added!"
            },
            "ytmusic_playlist_added": {
                "normal": "🎶 YouTube Music Playlist Added",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ YOUTUBE MUSIC PLAYLIST"
            },
            "ytmusic_playlist_description": {
                "normal": "**{count} tracks** being added...",
                "kawaii": "**{count} songs** added!"
            },
            "video_error": {
                "normal": "Error adding the video or playlist.",
                "kawaii": "(´；ω；`) Something went wrong with this video..."
            },
            "search_error": {
                "normal": "Error during search. Try another title.",
                "kawaii": "(︶︹︺) Couldn't find this song..."
            },
            "now_playing_title": {
                "normal": "🎵 Now Playing",
                "kawaii": "♫♬ NOW PLAYING ♬♫"
            },
            "now_playing_description": {
                "normal": "[{title}]({url})",
                "kawaii": "♪(´▽｀) [{title}]({url})"
            },
            "pause": {
                "normal": "⏸️ Playback paused.",
                "kawaii": "(´･_･`) Music paused..."
            },
            "no_playback": {
                "normal": "No playback in progress.",
                "kawaii": "(・_・;) Nothing is playing right now..."
            },
            "resume": {
                "normal": "▶️ Playback resumed.",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ Let's go again!"
            },
            "no_paused": {
                "normal": "No playback is paused.",
                "kawaii": "(´･ω･`) No music is paused..."
            },
            "skip": {
                "normal": "⏭️ Current song skipped.",
                "kawaii": "(ノ°ο°)ノ Skipped! Next song ~"
            },
            "no_song": {
                "normal": "No song is playing.",
                "kawaii": "(；一_一) Nothing to skip..."
            },
            "loop": {
                "normal": "🔁 Looping for the current song {state}.",
                "kawaii": "🔁 Looping for the current song {state}."
            },
            "loop_state_enabled": {
                "normal": "enabled",
                "kawaii": "enabled (◕‿◕✿)"
            },
            "loop_state_disabled": {
                "normal": "disabled",
                "kawaii": "disabled (¨_°`)"
            },
            "stop": {
                "normal": "⏹️ Playback stopped and bot disconnected.",
                "kawaii": "(ﾉ´･ω･)ﾉ ﾐ ┸━┸ All stopped! Bye bye ~"
            },
            "not_connected": {
                "normal": "The bot is not connected to a voice channel.",
                "kawaii": "(￣ω￣;) I'm not connected..."
            },
            "kawaii_toggle": {
                "normal": "Kawaii mode {state} for this server!",
                "kawaii": "Kawaii mode {state} for this server!"
            },
            "kawaii_state_enabled": {
                "normal": "enabled",
                "kawaii": "enabled (◕‿◕✿)"
            },
            "kawaii_state_disabled": {
                "normal": "disabled",
                "kawaii": "disabled"
            },
            "language_toggle": {
                "normal": "Language set to {lang} for this server!",
                "kawaii": "Language set to {lang} for this server! (✿◕‿◕)"
            },
            "shuffle_success": {
                "normal": "🔀 Queue shuffled successfully!",
                "kawaii": "(✿◕‿◕) Queue shuffled! Yay! ~"
            },
            "queue_empty": {
                "normal": "The queue is empty.",
                "kawaii": "(´･ω･`) No songs in the queue..."
            },
            "autoplay_toggle": {
                "normal": "Autoplay {state}.",
                "kawaii": "♫ Autoplay {state} (◕‿◕✿)"
            },
            "autoplay_state_enabled": {
                "normal": "enabled",
                "kawaii": "enabled"
            },
            "autoplay_state_disabled": {
                "normal": "disabled",
                "kawaii": "disabled"
            },
            "autoplay_added": {
                "normal": "🎵 Adding similar songs to the queue... (This may take up to 1 minute)",
                "kawaii": "♪(´▽｀) Adding similar songs to the queue! ~ (It might take a little while!)"
            },
            "queue_title": {
                "normal": "🎶 Queue",
                "kawaii": "🎶 Queue (◕‿◕✿)"
            },
            "queue_description": {
                "normal": "There are **{count} songs** in the queue.",
                "kawaii": "**{count} songs** in the queue! ~"
            },
            "queue_next": {
                "normal": "Next songs:",
                "kawaii": "Next songs: ♫"
            },
            "queue_song": {
                "normal": "- [{title}]({url})",
                "kawaii": "- ♪ [{title}]({url})"
            },
            "clear_queue_success": {
                "normal": "✅ Queue cleared.",
                "kawaii": "(≧▽≦) Queue cleared! ~"
            },
            "play_next_added": {
                "normal": "🎵 Added as next song",
                "kawaii": "(っ◕‿◕)っ ♫ Added as next song ♫"
            },
            "no_song_playing": {
                "normal": "No song is currently playing.",
                "kawaii": "(´･ω･`) No music is playing right now..."
            },
            "loading_playlist": {
                "normal": "Processing playlist...\n{processed}/{total} tracks added",
                "kawaii": "(✿◕‿◕) Processing playlist...\n{processed}/{total} songs added"
            },
            "playlist_error": {
                "normal": "Error processing the playlist. It may be private, region-locked, or invalid.",
                "kawaii": "(´；ω；`) Oh no! Problem with the playlist... maybe it’s shy or hidden?"
            },
        },
        "fr": {
            "no_voice_channel": {
                "normal": "Tu dois être dans un salon vocal pour utiliser cette commande.",
                "kawaii": "(｡•́︿•̀｡) Tu dois être dans un salon vocal !"
            },
            "connection_error": {
                "normal": "Erreur lors de la connexion au salon vocal.",
                "kawaii": "(╥﹏╥) Je n'ai pas pu me connecter..."
            },
            "spotify_error": {
                "normal": "Erreur lors du traitement du lien Spotify. Il peut être privé, restreint à une région ou invalide.",
                "kawaii": "(´；ω；`) Oh non ! Problème avec le lien Spotify... peut-être qu’il est timide ou caché ?"
            },
            "spotify_playlist_added": {
                "normal": "🎶 Playlist Spotify ajoutée",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ PLAYLIST SPOTIFY"
            },
            "spotify_playlist_description": {
                "normal": "**{count} chansons** ajoutées, {failed} échouées.\n{failed_tracks}",
                "kawaii": "**{count} chansons** ajoutées, {failed} n’ont pas pu rejoindre ! (´･ω･`)\n{failed_tracks}"
            },
            "deezer_error": {
                "normal": "Erreur lors du traitement du lien Deezer. Il peut être privé, restreint à une région ou invalide.",
                "kawaii": "(´；ω；`) Oh non ! Problème avec le lien Deezer... peut-être qu’il est timide ou caché ?"
            },
            "deezer_playlist_added": {
                "normal": "🎶 Playlist Deezer ajoutée",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ PLAYLIST DEEZER"
            },
            "deezer_playlist_description": {
                "normal": "**{count} chansons** ajoutées, {failed} échouées.\n{failed_tracks}",
                "kawaii": "**{count} chansons** ajoutées, {failed} n’ont pas pu rejoindre ! (´･ω･`)\n{failed_tracks}"
            },
            "apple_music_error": {
                "normal": "Erreur lors du traitement du lien Apple Music.",
                "kawaii": "(´；ω；`) Oups ! Problème avec le lien Apple Music..."
            },
            "apple_music_playlist_added": {
                "normal": "🎶 Playlist Apple Music ajoutée",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ PLAYLIST APPLE MUSIC"
            },
            "apple_music_playlist_description": {
                "normal": "**{count} chansons** ajoutées, {failed} échouées.\n{failed_tracks}",
                "kawaii": "**{count} chansons** ajoutées, {failed} n'ont pas pu rejoindre ! (´･ω･`)\n{failed_tracks}"
            },
            "tidal_error": {
                "normal": "Erreur lors du traitement du lien Tidal. Il peut être privé, restreint à une région ou invalide.",
                "kawaii": "(´；ω；`) Oh non ! Problème avec le lien Tidal... peut-être qu’il est timide ou caché ?"
            },
            "tidal_playlist_added": {
                "normal": "🎶 Playlist Tidal ajoutée",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ PLAYLIST TIDAL"
            },
            "tidal_playlist_description": {
                "normal": "**{count} chansons** ajoutées, {failed} échouées.\n{failed_tracks}",
                "kawaii": "**{count} chansons** ajoutées, {failed} n’ont pas pu rejoindre ! (´･ω･`)\n{failed_tracks}"
            },
            "amazon_music_error": {
                "normal": "Erreur lors du traitement du lien Amazon Music.",
                "kawaii": "(´；ω；`) Oh non ! Il y a un problème avec le lien Amazon Music..."
            },
            "amazon_music_playlist_added": {
                "normal": "🎶 Playlist Amazon Music ajoutée",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ PLAYLIST AMAZON MUSIC"
            },
            "amazon_music_playlist_description": {
                "normal": "**{count} pistes** ajoutées, {failed} échouées.\n{failed_tracks}",
                "kawaii": "**{count} chansons** ajoutées, {failed} n'ont pas pu être ajoutées ! (´･ω･`)\n{failed_tracks}"
            },
            "song_added": {
                "normal": "🎵 Ajoutée à la file",
                "kawaii": "(っ◕‿◕)っ ♫ MUSIQUE AJOUTÉE ♫"
            },
            "playlist_added": {
                "normal": "🎶 Playlist ajoutée",
                "kawaii": "✧･ﾟ: *✧･ﾟ:* PLAYLIST *:･ﾟ✧*:･ﾟ✧"
            },
            "playlist_description": {
                "normal": "**{count} chansons** ajoutées à la file d'attente.",
                "kawaii": "**{count} musiques** ajoutées !"
            },
            "ytmusic_playlist_added": {
                "normal": "🎶 Playlist YouTube Music ajoutée",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ PLAYLIST YOUTUBE MUSIC"
            },
            "ytmusic_playlist_description": {
                "normal": "**{count} chansons** en cours d'ajout...",
                "kawaii": "**{count} musiques** ajoutées !"
            },
            "video_error": {
                "normal": "Erreur lors de l'ajout de la vidéo ou de la playlist.",
                "kawaii": "(´；ω；`) Problème avec cette vidéo..."
            },
            "search_error": {
                "normal": "Erreur lors de la recherche. Essaye un autre titre.",
                "kawaii": "(¨_°`) Je n'ai pas trouvé cette musique..."
            },
            "now_playing_title": {
                "normal": "🎶 En cours de lecture",
                "kawaii": "♫♬ MAINTENANT EN LECTURE ♬♫"
            },
            "now_playing_description": {
                "normal": "[{title}]({url})",
                "kawaii": "♪(´▽｀) [{title}]({url})"
            },
            "pause": {
                "normal": "⏸️ Lecture mise en pause.",
                "kawaii": "(´･_･`) Musique en pause..."
            },
            "no_playback": {
                "normal": "Aucune lecture en cours.",
                "kawaii": "(・_・;) Rien ne joue actuellement..."
            },
            "resume": {
                "normal": "▶️ Lecture reprise.",
                "kawaii": "☆*:.｡.o(≧▽≦)o.｡.:*☆ C'est reparti !"
            },
            "no_paused": {
                "normal": "Aucune lecture mise en pause.",
                "kawaii": "(´･ω･`) Aucune musique en pause..."
            },
            "skip": {
                "normal": "⏭️ Chanson actuelle ignorée.",
                "kawaii": "(ノ°ο°)ノ Skippé ! Prochaine musique ~"
            },
            "no_song": {
                "normal": "Aucune chanson en cours.",
                "kawaii": "(；一_一) Rien à skipper..."
            },
            "loop": {
                "normal": "🔁 Lecture en boucle pour la musique actuelle {state}.",
                "kawaii": "🔁 Lecture en boucle pour la musique actuelle {state}."
            },
            "loop_state_enabled": {
                "normal": "activée",
                "kawaii": "activée (◕‿◕✿)"
            },
            "loop_state_disabled": {
                "normal": "désactivée",
                "kawaii": "désactivée (¨_°`)"
            },
            "stop": {
                "normal": "⏹️ Lecture arrêtée et bot déconnecté.",
                "kawaii": "(ﾉ´･ω･)ﾉ ﾐ ┸━┸ J'ai tout arrêté ! Bye bye ~"
            },
            "not_connected": {
                "normal": "Le bot n'est pas connecté à un salon vocal.",
                "kawaii": "(￣ω￣;) Je ne suis pas connecté..."
            },
            "kawaii_toggle": {
                "normal": "Mode kawaii {state} pour ce serveur !",
                "kawaii": "Mode kawaii {state} pour ce serveur !"
            },
            "kawaii_state_enabled": {
                "normal": "activé",
                "kawaii": "activé (◕‿◕✿)"
            },
            "kawaii_state_disabled": {
                "normal": "désactivé",
                "kawaii": "désactivé"
            },
            "language_toggle": {
                "normal": "Langue définie sur {lang} pour ce serveur !",
                "kawaii": "Langue définie sur {lang} pour ce serveur ! (✿◕‿◕)"
            },
            "shuffle_success": {
                "normal": "🔀 File d'attente mélangée avec succès !",
                "kawaii": "(✿◕‿◕) File d'attente mélangée ! Youpi ! ~"
            },
            "queue_empty": {
                "normal": "La file d'attente est vide.",
                "kawaii": "(´･ω･`) Pas de musiques dans la file..."
            },
            "autoplay_toggle": {
                "normal": "Autoplay {state}.",
                "kawaii": "♫ Autoplay {state} (◕‿◕✿)"
            },
            "autoplay_state_enabled": {
                "normal": "activé",
                "kawaii": "activé"
            },
            "autoplay_state_disabled": {
                "normal": "désactivé",
                "kawaii": "désactivé"
            },
            "autoplay_added": {
                "normal": "🎶 Ajout de chansons similaires à la file... (Cela peut prendre jusqu'à 1 minute)",
                "kawaii": "♪(´▽｀) Ajout de chansons similaires à la file ! ~ (Ça peut prendre un petit moment !)"
            },
            "queue_title": {
                "normal": "🎶 File d'attente",
                "kawaii": "🎶 File d'attente (◕‿◕✿)"
            },
            "queue_description": {
                "normal": "Il y a **{count} chansons** dans la file d'attente.",
                "kawaii": "**{count} chansons** dans la file d'attente ! ~"
            },
            "queue_next": {
                "normal": "Chansons suivantes :",
                "kawaii": "Chansons suivantes : ♫"
            },
            "queue_song": {
                "normal": "- [{title}]({url})",
                "kawaii": "- ♪ [{title}]({url})"
            },
            "clear_queue_success": {
                "normal": "✅ File d'attente effacée.",
                "kawaii": "(≧▽≦) File d'attente effacée ! ~"
            },
            "play_next_added": {
                "normal": "🎵 Ajoutée comme prochaine chanson",
                "kawaii": "(っ◕‿◕)っ ♫ Ajoutée comme prochaine chanson ♫"
            },
            "no_song_playing": {
                "normal": "Aucune chanson n'est actuellement en lecture.",
                "kawaii": "(´･ω･`) Aucune musique n'est en lecture actuellement..."
            },
            "loading_playlist": {
                "normal": "Traitement de la playlist...\n{processed}/{total} chansons ajoutées",
                "kawaii": "(✿◕‿◕) Traitement de la playlist...\n{processed}/{total} musiques ajoutées"
            },
            "playlist_error": {
                "normal": "Erreur lors du traitement de la playlist. Elle peut être privée, restreinte à une région ou invalide.",
                "kawaii": "(´；ω；`) Oh non ! Problème avec la playlist... peut-être qu’elle est timide ou cachée ?"
            },
        }
    }
    
    mode = "kawaii" if is_kawaii else "normal"
    return messages[lang][message_key][mode]

# YouTube Mix and SoundCloud Stations utilities
def get_video_id(url):
    parsed = urlparse(url)
    if parsed.hostname in ('www.youtube.com', 'youtube.com', 'youtu.be'):
        if parsed.hostname == 'youtu.be':
            return parsed.path[1:]
        if parsed.path == '/watch':
            query = parse_qs(parsed.query)
            return query.get('v', [None])[0]
    return None

def get_mix_playlist_url(video_url):
    video_id = get_video_id(video_url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    return None

def get_soundcloud_track_id(url):
    if "soundcloud.com" in url:
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get("id")
        except Exception:
            return None
    return None

def get_soundcloud_station_url(track_id):
    if track_id:
        return f"https://soundcloud.com/discover/sets/track-stations:{track_id}"
    return None

# --- FINAL FUNCTION PROCESS_SPOTIFY_URL (Cascade Architecture) ---
# THIS FUNCTION IS NOW CORRECT AND SHOULD NOT BE MODIFIED
async def process_spotify_url(url, interaction):
    """
    Process a Spotify URL using a cascade architecture:
    1. Try the official API (spotipy) for speed and completeness.
    2. If it fails (e.g., editorial playlist), switch to the scraper (spotifyscraper) as a backup.
    """
    guild_id = interaction.guild_id
    clean_url = url.split('?')[0]
    
    # --- METHOD 1: OFFICIAL API (SPOTIPY) ---
    if sp:
        try:
            logger.info(f"Attempt 1: Official API (Spotipy) for {clean_url}")
            tracks_to_return = []
            
            loop = asyncio.get_event_loop()
            
            if 'playlist' in clean_url:
                results = await loop.run_in_executor(None, lambda: sp.playlist_items(clean_url, fields='items.track.name,items.track.artists.name,next', limit=100))
                while results:
                    for item in results['items']:
                        if item and item.get('track'):
                            track = item['track']
                            tracks_to_return.append((track['name'], track['artists'][0]['name']))
                    if results['next']:
                        results = await loop.run_in_executor(None, lambda: sp.next(results))
                    else:
                        results = None
            
            elif 'album' in clean_url:
                results = await loop.run_in_executor(None, lambda: sp.album_tracks(clean_url, limit=50))
                while results:
                    for track in results['items']:
                        tracks_to_return.append((track['name'], track['artists'][0]['name']))
                    if results['next']:
                        results = await loop.run_in_executor(None, lambda: sp.next(results))
                    else:
                        results = None

            elif 'track' in clean_url:
                 track = await loop.run_in_executor(None, lambda: sp.track(clean_url))
                 tracks_to_return.append((track['name'], track['artists'][0]['name']))

            elif 'artist' in clean_url:
                results = await loop.run_in_executor(None, lambda: sp.artist_top_tracks(clean_url))
                for track in results['tracks']:
                    tracks_to_return.append((track['name'], track['artists'][0]['name']))

            if not tracks_to_return:
                 raise ValueError("No tracks found via API.")

            logger.info(f"Success with Spotipy : {len(tracks_to_return)} tracks recovered.")
            return tracks_to_return

        except Exception as e:
            logger.warning(f"Spotipy API failed for {clean_url} (Reason: {e}). Moving on to plan B: SpotifyScraper.")

    # --- METHOD 2: RESCUE (SPOTIFYSCRAPER) ---
    if spotify_scraper_client:
        try:
            logger.info(f"Attempt 2: Scraper (SpotifyScraper) to {clean_url}")
            tracks_to_return = []
            loop = asyncio.get_event_loop()

            if 'playlist' in clean_url:
                data = await loop.run_in_executor(None, lambda: spotify_scraper_client.get_playlist_info(clean_url))
                for track in data.get('tracks', []):
                    tracks_to_return.append((track.get('name', 'Titre inconnu'), track.get('artists', [{}])[0].get('name', 'Artiste inconnu')))
            
            elif 'album' in clean_url:
                data = await loop.run_in_executor(None, lambda: spotify_scraper_client.get_album_info(clean_url))
                for track in data.get('tracks', []):
                    tracks_to_return.append((track.get('name', 'Titre inconnu'), track.get('artists', [{}])[0].get('name', 'Artiste inconnu')))

            elif 'track' in clean_url:
                data = await loop.run_in_executor(None, lambda: spotify_scraper_client.get_track_info(clean_url))
                tracks_to_return.append((data.get('name', 'Titre inconnu'), data.get('artists', [{}])[0].get('name', 'Artiste inconnu')))

            if not tracks_to_return:
                raise SpotifyScraperError("The scraper didn't find any leads either.")

            logger.info(f"Success with SpotifyScraper : {len(tracks_to_return)} tracks recovered (potentially limited).")
            return tracks_to_return

        except Exception as e:
            logger.error(f"Both methods (API and Scraper) failed. Final error from SpotifyScraper: {e}", exc_info=True)
            embed = Embed(description=get_messages("spotify_error", guild_id), color=0xFFB6C1 if get_mode(guild_id) else discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return None

    logger.critical("No client (Spotipy or SpotifyScraper) is functional.")
    # Optional: Send an error message if no client is available
    embed = Embed(description="Critical error: Spotify services are inaccessible.", color=discord.Color.dark_red())
    await interaction.followup.send(embed=embed, ephemeral=True)
    return None
    
# Process Deezer URLs
async def process_deezer_url(url, interaction):
    guild_id = interaction.guild_id
    try:
        # Check if it is a sharing link
        deezer_share_regex = re.compile(r'^(https?://)?(link\.deezer\.com)/s/.+$')
        if deezer_share_regex.match(url):
            logger.info(f"Detected Deezer share link: {url}. Resolving redirect...")
            response = requests.head(url, allow_redirects=True, timeout=10)
            response.raise_for_status()  # Check if the request was successful
            resolved_url = response.url
            logger.info(f"Resolved to: {resolved_url}")
            url = resolved_url  # Replace with the resolved URL

        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip('/').split('/')
        if len(path_parts) > 1 and len(path_parts[0]) == 2:
            path_parts = path_parts[1:]
        if len(path_parts) < 2:
            raise ValueError("Invalid Deezer URL format")

        resource_type = path_parts[0]
        resource_id = path_parts[1].split('?')[0]
        
        base_api_url = "https://api.deezer.com"
        logger.info(f"Fetching Deezer {resource_type} with ID {resource_id} from URL {url}")

        tracks = []
        if resource_type == 'track':
            response = requests.get(f"{base_api_url}/track/{resource_id}", timeout=10)
            response.raise_for_status()
            data = response.json()
            if 'error' in data:
                raise Exception(f"Deezer API error: {data['error']['message']}")
            logger.info(f"Processing Deezer track: {data.get('title', 'Unknown Title')}")
            track_name = data.get('title', 'Unknown Title')
            artist_name = data.get('artist', {}).get('name', 'Unknown Artist')
            tracks.append((track_name, artist_name))

        elif resource_type == 'playlist':
            next_url = f"{base_api_url}/playlist/{resource_id}/tracks"
            total_tracks = 0
            fetched_tracks = 0
            
            while next_url:
                response = requests.get(next_url, timeout=10)
                response.raise_for_status()
                data = response.json()
                
                if 'error' in data:
                    raise Exception(f"Deezer API error: {data['error']['message']}")
                
                if not data.get('data'):
                    raise ValueError("No tracks found in the playlist or playlist is empty")
                
                for track in data['data']:
                    track_name = track.get('title', 'Unknown Title')
                    artist_name = track.get('artist', {}).get('name', 'Unknown Artist')
                    tracks.append((track_name, artist_name))
                
                fetched_tracks += len(data['data'])
                total_tracks = data.get('total', fetched_tracks)
                logger.info(f"Fetched {fetched_tracks}/{total_tracks} tracks from playlist {resource_id}")
                
                next_url = data.get('next')
                if next_url:
                    logger.info(f"Fetching next page: {next_url}")
                
            logger.info(f"Processing Deezer playlist: {data.get('title', 'Unknown Playlist')} with {len(tracks)} tracks")
        
        elif resource_type == 'album':
            response = requests.get(f"{base_api_url}/album/{resource_id}/tracks", timeout=10)
            response.raise_for_status()
            data = response.json()
            if 'error' in data:
                raise Exception(f"Deezer API error: {data['error']['message']}")
            if not data.get('data'):
                raise ValueError("No tracks found in the album or album is empty")
            logger.info(f"Processing Deezer album: {data.get('title', 'Unknown Album')}")
            for track in data['data']:
                track_name = track.get('title', 'Unknown Title')
                artist_name = track.get('artist', {}).get('name', 'Unknown Artist')
                tracks.append((track_name, artist_name))
            logger.info(f"Extracted {len(tracks)} tracks from album {resource_id}")
        
        elif resource_type == 'artist':
            response = requests.get(f"{base_api_url}/artist/{resource_id}/top?limit=10", timeout=10)
            response.raise_for_status()
            data = response.json()
            if 'error' in data:
                raise Exception(f"Deezer API error: {data['error']['message']}")
            if not data.get('data'):
                raise ValueError("No top tracks found for the artist")
            logger.info(f"Processing Deezer artist: {data.get('name', 'Unknown Artist')}")
            for track in data['data']:
                track_name = track.get('title', 'Unknown Title')
                artist_name = track.get('artist', {}).get('name', 'Unknown Artist')
                tracks.append((track_name, artist_name))
            logger.info(f"Extracted {len(tracks)} top tracks for artist {resource_id}")
        
        if not tracks:
            raise ValueError("No valid tracks found in the Deezer resource")
        
        logger.info(f"Successfully processed Deezer {resource_type} with {len(tracks)} tracks")
        return tracks

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching Deezer URL {url}: {e}")
        embed = Embed(
            description="Network error while retrieving Deezer data. Try again later..",
            color=0xFFB6C1 if get_mode(guild_id) else discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return None
    except ValueError as e:
        logger.error(f"Invalid Deezer data for URL {url}: {e}")
        embed = Embed(
            description=f"Erreur : {str(e)}",
            color=0xFFB6C1 if get_mode(guild_id) else discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return None
    except Exception as e:
        logger.error(f"Unexpected error processing Deezer URL {url}: {e}")
        embed = Embed(
            description=get_messages("deezer_error", guild_id),
            color=0xFFB6C1 if get_mode(guild_id) else discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return None
    
# Process Apple Music URLs
async def process_apple_music_url(url, interaction):
    guild_id = interaction.guild_id
    try:
        parsed_url = urlparse(url)
        clean_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        path_parts = parsed_url.path.strip('/').split('/')
        if len(path_parts) < 3 or "music.apple.com" not in parsed_url.netloc:
            raise ValueError("Invalid Apple Music URL format")
        
        resource_type = path_parts[1]
        logger.info(f"Processing Apple Music URL: {clean_url} (Type: {resource_type})")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
            await page.goto(clean_url, wait_until="networkidle")
            await asyncio.sleep(2)

            tracks = []
            
            if resource_type in ['album', 'playlist']:
                logger.info("Album/playlist detected. Searching for track list...")
                await page.wait_for_selector('div.songs-list-row', timeout=15000)
                track_rows = await page.query_selector_all('div.songs-list-row')

                # --- NEW: Get the main artist of the album first as a fallback ---
                main_artist_name = "Artiste inconnu"
                try:
                    main_artist_selector = '.headings__subtitles a'
                    main_artist_el = await page.query_selector(main_artist_selector)
                    if main_artist_el:
                        main_artist_name = await main_artist_el.inner_text()
                        logger.info(f"Main artist of the album identified as: {main_artist_name}")
                except Exception:
                    logger.warning("Could not determine the main artist of the album.")


                for row in track_rows:
                    try:
                        title_el = await row.query_selector('div.songs-list-row__song-name')
                        title = await title_el.inner_text() if title_el else "Titre inconnu"

                        # --- NEW: More robust artist detection ---
                        # Method 1: Look for specific artist links within the row (for collaborations)
                        artist_elements = await row.query_selector_all('div.songs-list-row__by-line a')
                        if artist_elements:
                            # Join multiple artists if present, e.g., "KAROL G & Eddy Lover"
                            artist_names = [await el.inner_text() for el in artist_elements]
                            artist = " & ".join(artist_names)
                        else:
                            # Method 2: If no specific artist in the row, use the main album artist
                            artist = main_artist_name
                        
                        if title != "Titre inconnu":
                            tracks.append((title.strip(), artist.strip()))
                    except Exception as e:
                        logger.warning(f"Could not extract a row from the Apple Music list: {e}")

            elif resource_type == 'song':
                logger.info("Single song detected. Extracting information...")
                try:
                    title_selector = 'h1.song-header-page__song-header-title'
                    artist_selector = 'div.song-header-page-details a.click-action'
                    await page.wait_for_selector(title_selector, timeout=10000)
                    title = await page.locator(title_selector).first.inner_text()
                    artist = await page.locator(artist_selector).first.inner_text()
                    if title and artist:
                        tracks.append((title.strip(), artist.strip()))
                except Exception:
                    logger.warning("Direct extraction failed, trying fallback with page title...")
                    try:
                        page_title = await page.title()
                        parts = page_title.split(' par ')
                        title = parts[0].replace("‎", "").strip()
                        artist = parts[1].split(' sur Apple')[0].strip()
                        if title and artist:
                            tracks.append((title, artist))
                    except Exception as fallback_e:
                        raise ValueError(f"All extraction methods for the song failed. Final error: {fallback_e}")
            
            else:
                raise ValueError(f"Unsupported Apple Music resource type: {resource_type}")

            await browser.close()
            if not tracks:
                raise ValueError("No tracks found in the Apple Music resource")
            
            logger.info(f"Successfully extracted {len(tracks)} track(s).")
            return tracks

    except Exception as e:
        logger.error(f"Error processing Apple Music URL {url}: {e}")
        embed = Embed(
            description=get_messages("apple_music_error", guild_id),
            color=0xFFB6C1 if get_mode(guild_id) else discord.Color.red()
        )
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.errors.InteractionResponded:
            await interaction.edit_original_response(embed=embed)
        return None
                                
# Process Tidal URLs
async def process_tidal_url(url, interaction):
    guild_id = interaction.guild_id

    # --- The internal function for lists remains unchanged ---
    async def load_and_extract_all_tracks(page):
        logger.info("Reliable start of loading (track by track)...")
        total_tracks_expected = 0
        try:
            meta_item_selector = 'span[data-test="grid-item-meta-item-count"]'
            meta_text = await page.locator(meta_item_selector).first.inner_text(timeout=3000)
            total_tracks_expected = int(re.search(r'\d+', meta_text).group())
            logger.info(f"Objective: Extract {total_tracks_expected} tracks.")
        except Exception:
            logger.warning("Unable to determine the total number of tracks.")
            total_tracks_expected = 0
        track_row_selector = 'div[data-track-id]'
        all_tracks = []
        seen_track_ids = set()
        stagnation_counter = 0
        max_loops = 500
        for i in range(max_loops):
            if total_tracks_expected > 0 and len(all_tracks) >= total_tracks_expected:
                logger.info("All expected leads have been found. Early stop.")
                break
            track_elements = await page.query_selector_all(track_row_selector)
            if not track_elements and i > 0: break
            new_tracks_found_in_loop = False
            for element in track_elements:
                track_id = await element.get_attribute('data-track-id')
                if track_id and track_id not in seen_track_ids:
                    new_tracks_found_in_loop = True
                    seen_track_ids.add(track_id)
                    try:
                        title_el = await element.query_selector('span._titleText_51cccae, span[data-test="table-cell-title"]')
                        artist_el = await element.query_selector('a._item_39605ae, a[data-test="grid-item-detail-text-title-artist"]')
                        if title_el and artist_el:
                            title = (await title_el.inner_text()).split("<span>")[0].strip()
                            artist = await artist_el.inner_text()
                            if title and artist: all_tracks.append((title, artist))
                    except Exception: continue
            if not new_tracks_found_in_loop and i > 1:
                stagnation_counter += 1
                if stagnation_counter >= 5:
                    logger.info("Stable stagnation. End of process..")
                    break
            else: stagnation_counter = 0
            if track_elements:
                await track_elements[-1].scroll_into_view_if_needed(timeout=10000)
                await asyncio.sleep(0.75)
        logger.info(f"Process completed. Final total of unique tracks extracted. : {len(all_tracks)}")
        return list(dict.fromkeys(all_tracks))

    # --- Main logic ---
    try:
        clean_url = url.split('?')[0]
        parsed_url = urlparse(clean_url)
        path_parts = parsed_url.path.strip('/').split('/')
        
        resource_type = None
        if 'playlist' in path_parts: resource_type = 'playlist'
        elif 'album' in path_parts: resource_type = 'album'
        elif 'mix' in path_parts: resource_type = 'mix'
        elif 'track' in path_parts: resource_type = 'track'
        elif 'video' in path_parts: resource_type = 'video'

        if resource_type is None:
            raise ValueError("Tidal URL not supported.")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
            await page.goto(clean_url, wait_until="domcontentloaded") # Using domcontentloaded is faster
            logger.info(f"Navigating to the Tidal URL ({resource_type}) : {clean_url}")
            
            # Initial wait a little longer for complex pages
            await asyncio.sleep(3)
            unique_tracks = []

            if resource_type in ['playlist', 'album', 'mix']:
                unique_tracks = await load_and_extract_all_tracks(page)
            
            elif resource_type == 'track' or resource_type == 'video':
                logger.info(f"Extracting a single media ({resource_type})...")
                try:
                    # For titles and videos, we use a more direct method
                    # which does not depend on the strict "visibility" of the element.
                    
                    # We're just waiting for the main container to arrive.
                    await page.wait_for_selector('div[data-test="artist-profile-header"], div[data-test="footer-player"]', timeout=10000)

                    title_selector = 'span[data-test="now-playing-track-title"], h1[data-test="title"]'
                    artist_selector = 'a[data-test="grid-item-detail-text-title-artist"]'
                    
                    # We take the text of the first element found directly, without waiting for its "visibility"
                    title = await page.locator(title_selector).first.inner_text(timeout=5000)
                    artist = await page.locator(artist_selector).first.inner_text(timeout=5000)
                    
                    if not title or not artist:
                        raise ValueError("Missing title or artist.")
                    
                    logger.info(f"Unique media found : {title.strip()} - {artist.strip()}")
                    unique_tracks = [(title.strip(), artist.strip())]

                except Exception as e:
                    # If the direct method fails, the page title method is tried as a last resort.
                    logger.warning(f"Direct extraction method failed ({e}), attempt with page title...")
                    try:
                        page_title = await page.title()
                        title, artist = "", ""
                        if " - " in page_title:
                            parts = page_title.split(' - ')
                            artist, title = parts[0], parts[1].split(' on TIDAL')[0]
                        elif " by " in page_title:
                            parts = page_title.split(' by ')
                            title, artist = parts[0], parts[1].split(' on TIDAL')[0]
                        
                        if not title or not artist: raise ValueError("The page title format is unknown.")
                        
                        logger.info(f"Unique media found via page title : {title.strip()} - {artist.strip()}")
                        unique_tracks = [(title.strip(), artist.strip())]
                    except Exception as fallback_e:
                        await page.screenshot(path=f"tidal_{resource_type}_extraction_failed.png")
                        raise ValueError(f"All extraction methods failed. Final error.: {fallback_e}")
            
            if not unique_tracks:
                raise ValueError("No tracks could be retrieved from the Tidal resource.")

            return unique_tracks

    except Exception as e:
        logger.error(f"Major error in process_tidal_url for {url}: {e}")
        if interaction:
             embed = Embed(description=get_messages("tidal_error", guild_id), color=0xFFB6C1 if get_mode(guild_id) else discord.Color.red())
             await interaction.followup.send(embed=embed, ephemeral=True)
        return None
                                                                                                        

async def process_amazon_music_url(url, interaction):
    guild_id = interaction.guild_id
    logger.info(f"Launch of unified processing for Amazon Music URL : {url}")
    
    # Step 1: Determine the type of link
    is_album = "/albums/" in url
    is_playlist = "/playlists/" in url or "/user-playlists/" in url
    is_track = "/tracks/" in url

    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'
            )
            page = await context.new_page()
            
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            logger.info("Page loaded. Cookie management.")
            
            try:
                await page.click('music-button:has-text("Accepter les cookies")', timeout=7000)
                logger.info("Cookie banner accepted.")
            except Exception:
                logger.info("No cookie banner found.")

            tracks = []

            # --- STEP 2: GUIDANCE TO THE CORRECT EXTRACTION METHOD ---

            if is_album or is_track:
                # ======================================================
                # METHOD FOR ALBUMS AND TRACKS (via JSON-LD)
                # ======================================================
                page_type = "Album" if is_album else "Track"
                logger.info(f"Type page '{page_type}' detected. Using the JSON extraction method.")
                
                selector = 'script[type="application/ld+json"]'
                await page.wait_for_selector(selector, state='attached', timeout=20000)
                
                json_ld_scripts = await page.locator(selector).all_inner_texts()
                
                found_data = False
                for script_content in json_ld_scripts:
                    data = json.loads(script_content)
                    if data.get('@type') == 'MusicAlbum' or (is_album and 'itemListElement' in data):
                        album_artist = data.get('byArtist', {}).get('name', 'Artiste inconnu')
                        for item in data.get('itemListElement', []):
                            track_name = item.get('name')
                            track_artist = item.get('byArtist', {}).get('name', album_artist)
                            if track_name and track_artist:
                                tracks.append((track_name, track_artist))
                        found_data = True
                        break 
                    elif data.get('@type') == 'MusicRecording':
                        track_name = data.get('name')
                        track_artist = data.get('byArtist', {}).get('name', 'Artiste inconnu')
                        if track_name and track_artist:
                            tracks.append((track_name, track_artist))
                        found_data = True
                        break
                
                if not found_data:
                    raise ValueError(f"No data of type 'MusicAlbum' or 'MusicRecording' found in JSON-LD tags.")

            elif is_playlist:
                # ======================================================
                # METHOD FOR PLAYLISTS (Quick Extraction)
                # ======================================================
                logger.info("Page of type 'Playlist' detected. Using fast extraction before virtualization.")
                try:
                    await page.wait_for_selector("music-image-row[primary-text]", timeout=20000)
                    logger.info("Tracklist detected. Waiting 3.5 seconds for initial load..")
                    await asyncio.sleep(3.5) # Temps crucial
                except Exception as e:
                    raise ValueError(f"Unable to detect initial tracklist : {e}")

                js_script_playlist = """
                () => {
                    const tracksData = [];
                    const rows = document.querySelectorAll('music-image-row[primary-text]');
                    rows.forEach(row => {
                        const title = row.getAttribute('primary-text');
                        const artist = row.getAttribute('secondary-text-1');
                        const indexEl = row.querySelector('span.index');
                        const index = indexEl ? parseInt(indexEl.innerText.trim(), 10) : null;
                        if (title && artist && index !== null && !isNaN(index)) {
                            tracksData.push({ index: index, title: title.trim(), artist: artist.trim() });
                        }
                    });
                    tracksData.sort((a, b) => a.index - b.index);
                    return tracksData.map(t => ({ title: t.title, artist: t.artist }));
                }
                """
                tracks_data = await page.evaluate(js_script_playlist)
                tracks = [(track['title'], track['artist']) for track in tracks_data]

            else:
                raise ValueError("Amazon Music URL not recognized (neither album, nor playlist, nor track).")

            if not tracks:
                raise ValueError("No tracks could be extracted from the page.")

            logger.info(f"Processing completed. {len(tracks)} track(s) found. First track: {tracks[0]}")
            return tracks

    except Exception as e:
        logger.error(f"Final error in process_amazon_music_url for {url}: {e}", exc_info=True)
        if 'page' in locals() and page and not page.is_closed():
             await page.screenshot(path="amazon_music_scrape_failed.png")
             logger.info("Screenshot of the error saved.")
        
        embed = Embed(description=get_messages("amazon_music_error", guild_id), color=0xFFB6C1 if get_mode(guild_id) else discord.Color.red())
        try:
            if interaction and not interaction.is_expired():
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as send_error:
            logger.error(f"Unable to send error message : {send_error}")
        return None
    finally:
        if browser:
            await browser.close()
            logger.info("Playwright Browser Closed.")
        
# /kaomoji command
@bot.tree.command(name="kaomoji", description="Enable/disable kawaii mode")
@app_commands.default_permissions(administrator=True)
async def toggle_kawaii(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    kawaii_mode[guild_id] = not get_mode(guild_id)
    state = get_messages("kawaii_state_enabled", guild_id) if kawaii_mode[guild_id] else get_messages("kawaii_state_disabled", guild_id)
    
    embed = Embed(
        description=get_messages("kawaii_toggle", guild_id).format(state=state),
        color=0xFFB6C1 if kawaii_mode[guild_id] else discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# /language command
@bot.tree.command(name="language", description="Set the bot's language for this server")
@app_commands.default_permissions(administrator=True)
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Français", value="fr")
])
async def set_language(interaction: discord.Interaction, language: str):
    guild_id = interaction.guild_id
    server_languages[guild_id] = language
    lang_name = "English" if language == "en" else "Français"
    
    embed = Embed(
        description=get_messages("language_toggle", guild_id).format(lang=lang_name),
        color=0xFFB6C1 if get_mode(guild_id) else discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="play", description="Play a link or search for a song")
@app_commands.describe(query="Link or title of the song/video to play")
async def play(interaction: discord.Interaction, query: str):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if not interaction.user.voice or not interaction.user.voice.channel:
        embed = Embed(
            description=get_messages("no_voice_channel", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if not music_player.voice_client or not music_player.voice_client.is_connected():
        try:
            music_player.voice_client = await interaction.user.voice.channel.connect()
        except Exception as e:
            embed = Embed(
                description=get_messages("connection_error", guild_id),
                color=0xFF9AA2 if is_kawaii else discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.error(f"Error: {e}")
            return

    music_player.text_channel = interaction.channel
    await interaction.response.defer()

    spotify_regex = re.compile(r'^(https?://)?(open\.spotify\.com)/.+$')
    deezer_regex = re.compile(r'^(https?://)?((www\.)?deezer\.com/(?:[a-z]{2}/)?(track|playlist|album|artist)/.+|(link\.deezer\.com)/s/.+)$')
    soundcloud_regex = re.compile(r'^(https?://)?(www\.)?(soundcloud\.com)/.+$')
    youtube_regex = re.compile(r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+$')
    ytmusic_regex = re.compile(r'^(https?://)?(music\.youtube\.com)/.+$')
    bandcamp_regex = re.compile(r'^(https?://)?([^\.]+)\.bandcamp\.com/.+$')
    apple_music_regex = re.compile(r'^(https?://)?(music\.apple\.com)/.+$')
    tidal_regex = re.compile(r'^(https?://)?(www\.)?tidal\.com/.+$')
    amazon_music_regex = re.compile(r'^(https?://)?(music\.amazon\.(fr|com|co\.uk|de|es|it|jp))/.+$')

    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "no_color": True,
        "socket_timeout": 10,
        "force_generic_extractor": True,
        "cookiefile": "/mnt/server/cookies.txt"
    }

    async def search_track(track):
        track_name, artist_name = track
        original_query = f"{track_name} {artist_name}"
        sanitized_query = sanitize_query(original_query)
        cache_key = sanitized_query.lower()
        logger.info(f"Searching YouTube for: {original_query} (Sanitized: {sanitized_query})")
        
        try:
            if cache_key in url_cache:
                logger.info(f"Cache hit for {cache_key}")
                return cache_key, url_cache[cache_key], track_name, artist_name
            
            ydl_opts_search = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "noplaylist": True,
                "no_color": True,
                "socket_timeout": 10,
                "cookiefile": "/mnt/server/cookies.txt"
            }
            
            search_query = f"ytsearch:{sanitized_query}"
            info = await extract_info_async(ydl_opts_search, search_query)
            
            if "entries" in info and info["entries"]:
                for entry in info["entries"][:3]:
                    try:
                        video_url = entry["url"]
                        video_title = entry.get("title", "Unknown Title")
                        logger.info(f"Found YouTube URL: {video_url} (Title: {video_title})")
                        url_cache[cache_key] = video_url
                        return cache_key, video_url, track_name, artist_name
                    except Exception as e:
                        logger.warning(f"Skipping unavailable video for {sanitized_query}: {e}")
                        continue
                logger.warning(f"No accessible videos in top results for {sanitized_query}")
            
            sanitized_track = sanitize_query(track_name)
            logger.info(f"Fallback search: {sanitized_track}")
            search_query = f"ytsearch:{sanitized_track}"
            info = await extract_info_async(ydl_opts_search, search_query)
            if "entries" in info and info["entries"]:
                for entry in info["entries"][:3]:
                    try:
                        video_url = entry["url"]
                        video_title = entry.get("title", "Unknown Title")
                        logger.info(f"Found YouTube URL: {video_url} (Title: {video_title})")
                        url_cache[cache_key] = video_url
                        return cache_key, video_url, track_name, artist_name
                    except Exception as e:
                        logger.warning(f"Skipping unavailable video for {sanitized_track}: {e}")
                        continue
                logger.warning(f"No accessible videos in top results for {sanitized_track}")
            
            sanitized_artist = sanitize_query(artist_name)
            logger.info(f"Fallback search: {sanitized_artist}")
            search_query = f"ytsearch:{sanitized_artist}"
            info = await extract_info_async(ydl_opts_search, search_query)
            if "entries" in info and info["entries"]:
                for entry in info["entries"][:3]:
                    try:
                        video_url = entry["url"]
                        video_title = entry.get("title", "Unknown Title")
                        logger.info(f"Found YouTube URL: {video_url} (Title: {video_title})")
                        url_cache[cache_key] = video_url
                        return cache_key, video_url, track_name, artist_name
                    except Exception as e:
                        logger.warning(f"Skipping unavailable video for {sanitized_artist}: {e}")
                        continue
                logger.warning(f"No accessible videos in top results for {sanitized_artist}")
            
            logger.warning(f"No YouTube results for {sanitized_query}, {sanitized_track}, or {sanitized_artist}")
            url_cache[cache_key] = None
            return cache_key, None, track_name, artist_name
        
        except Exception as e:
            logger.error(f"Failed to search YouTube for {sanitized_query}: {e}")
            url_cache[cache_key] = None
            return cache_key, None, track_name, artist_name

    if spotify_regex.match(query):
        spotify_tracks = await process_spotify_url(query, interaction)
        if not spotify_tracks:
            return

        if len(spotify_tracks) == 1:
            track_name, artist_name = spotify_tracks[0]
            query = f"{track_name} {artist_name}"
            try:
                cache_key = sanitize_query(query).lower()
                ydl_opts_full = {
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "no_color": True,
                    "socket_timeout": 10,
                    "force_generic_extractor": True,
                    "cookiefile": "/mnt/server/cookies.txt"
                }
                sanitized_query = sanitize_query(query)
                search_query = f"ytsearch:{sanitized_query}"
                info = await extract_info_async(ydl_opts_full, search_query)
                video = info["entries"][0] if "entries" in info and info["entries"] else None
                if not video:
                    raise Exception("No results found")
                video_url = video.get("webpage_url", video.get("url"))
                if not video_url:
                    raise KeyError("No valid URL found in video metadata")
                logger.debug(f"Metadata for single track: {video}")
                url_cache[cache_key] = video_url
                await music_player.queue.put({'url': video_url, 'is_single': True, 'skip_now_playing': True})
                embed = Embed(
                    title=get_messages("song_added", guild_id),
                    description=f"[{video.get('title', track_name)}]({video_url})",
                    color=0xC7CEEA if is_kawaii else discord.Color.blue()
                )
                if video.get("thumbnail"):
                    embed.set_thumbnail(url=video["thumbnail"])
                if is_kawaii:
                    embed.set_footer(text="☆⌒(≧▽° )")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                logger.error(f"Spotify conversion error for {query}: {e}")
                embed = Embed(
                    description=get_messages("search_error", guild_id),
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = Embed(
                title="Spotify playlist processing",
                description="Starting...",
                color=0xFFB6C1 if is_kawaii else discord.Color.blue()
            )
            message = await interaction.followup.send(embed=embed)
            
            total_tracks = len(spotify_tracks)
            processed = 0
            failed = 0
            failed_tracks = []
            batch_size = 50
            
            for i in range(0, total_tracks, batch_size):
                batch = spotify_tracks[i:i + batch_size]
                tasks = [search_track(track) for track in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for result in results:
                    if isinstance(result, Exception) or result[1] is None:
                        failed += 1
                        if len(failed_tracks) < 5:
                            failed_tracks.append(f"{result[2]} par {result[3]}")
                    else:
                        _, video_url, _, _ = result
                        await music_player.queue.put({'url': video_url, 'is_single': False})
                    processed += 1
                    
                    if processed % 10 == 0 or processed == total_tracks:
                        progress = processed / total_tracks
                        bar = create_loading_bar(progress)
                        description = f"{bar}\n" + get_messages("loading_playlist", guild_id).format(
                            processed=processed,
                            total=total_tracks
                        )
                        embed.description = description
                        await message.edit(embed=embed)
                await asyncio.sleep(0.1)
            
            logger.info(f"Spotify playlist processed : {processed - failed} tracks added, {failed} failed")
            
            if processed - failed == 0:
                embed = Embed(
                    description="⚠️ No tracks could be added.",
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await message.edit(embed=embed)
                return

            failed_text = "\nFailed tracks (up to 5) :\n" + "\n".join([f"- {track}" for track in failed_tracks]) if failed_tracks else ""
            embed.title = get_messages("spotify_playlist_added", guild_id)
            embed.description = get_messages("spotify_playlist_description", guild_id).format(
                count=processed - failed,
                failed=failed,
                failed_tracks=failed_text
            )
            embed.color = 0xB5EAD7 if is_kawaii else discord.Color.green()
            await message.edit(embed=embed)
    elif deezer_regex.match(query):
        logger.info(f"Processing Deezer URL: {query}")
        deezer_tracks = await process_deezer_url(query, interaction)
        if not deezer_tracks:
            logger.warning(f"No tracks returned for Deezer URL: {query}")
            embed = Embed(
                description="No Deezer tracks could be processed. Check the URL or try again..",
                color=0xFFB6C1 if is_kawaii else discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if len(deezer_tracks) == 1:
            track_name, artist_name = deezer_tracks[0]
            query = f"{track_name} {artist_name}"
            logger.info(f"Searching YouTube for single Deezer track: {query}")
            try:
                cache_key = sanitize_query(query).lower()
                ydl_opts_full = {
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "no_color": True,
                    "socket_timeout": 10,
                    "force_generic_extractor": True,
                    "cookiefile": "/mnt/server/cookies.txt"
                }
                sanitized_query = sanitize_query(query)
                search_query = f"ytsearch:{sanitized_query}"
                info = await extract_info_async(ydl_opts_full, search_query)
                video = info["entries"][0] if "entries" in info and info["entries"] else None
                if not video:
                    raise Exception(f"No YouTube results found for {sanitized_query}")
                video_url = video.get("webpage_url", video.get("url"))
                if not video_url:
                    raise KeyError("No valid URL found in video metadata")
                logger.info(f"Found YouTube URL for Deezer track: {video_url}")
                url_cache[cache_key] = video_url
                await music_player.queue.put({'url': video_url, 'is_single': True, 'skip_now_playing': True})
                embed = Embed(
                    title=get_messages("song_added", guild_id),
                    description=f"[{video.get('title', track_name)}]({video_url})",
                    color=0xC7CEEA if is_kawaii else discord.Color.blue()
                )
                if video.get("thumbnail"):
                    embed.set_thumbnail(url=video["thumbnail"])
                if is_kawaii:
                    embed.set_footer(text="☆⌒(≧▽° )")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                logger.error(f"Error converting Deezer track to YouTube for {query}: {e}")
                embed = Embed(
                    description=get_messages("search_error", guild_id),
                    color=0xFFB6C1 if is_kawaii else discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = Embed(
                title="Deezer playlist processing",
                description="Starting...",
                color=0xFFB6C1 if is_kawaii else discord.Color.blue()
            )
            message = await interaction.followup.send(embed=embed)
            
            total_tracks = len(deezer_tracks)
            processed = 0
            failed = 0
            failed_tracks = []
            batch_size = 50
            
            logger.info(f"Processing {total_tracks} tracks from Deezer playlist")
            for i in range(0, total_tracks, batch_size):
                batch = deezer_tracks[i:i + batch_size]
                tasks = [search_track(track) for track in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for result in results:
                    if isinstance(result, Exception) or result[1] is None:
                        failed += 1
                        if len(failed_tracks) < 5:
                            failed_tracks.append(f"{result[2]} par {result[3]}")
                    else:
                        _, video_url, _, _ = result
                        await music_player.queue.put({'url': video_url, 'is_single': False})
                    processed += 1
                    
                    if processed % 10 == 0 or processed == total_tracks:
                        progress = processed / total_tracks
                        bar = create_loading_bar(progress)
                        description = f"{bar}\n" + get_messages("loading_playlist", guild_id).format(
                            processed=processed,
                            total=total_tracks
                        )
                        embed.description = description
                        await message.edit(embed=embed)
                await asyncio.sleep(0.1)
            
            logger.info(f"Processed Deezer playlist : {processed - failed} tracks added, {failed} failed")
            
            if processed - failed == 0:
                embed = Embed(
                    description="⚠️ No tracks could be added to the queue.",
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await message.edit(embed=embed)
                return

            failed_text = "\nFailed tracks (up to 5) :\n" + "\n".join([f"- {track}" for track in failed_tracks]) if failed_tracks else ""
            embed.title = get_messages("deezer_playlist_added", guild_id)
            embed.description = get_messages("deezer_playlist_description", guild_id).format(
                count=processed - failed,
                failed=failed,
                failed_tracks=failed_text
            )
            embed.color = 0xB5EAD7 if is_kawaii else discord.Color.green()
            await message.edit(embed=embed)
    elif apple_music_regex.match(query):
        apple_tracks = await process_apple_music_url(query, interaction)
        if not apple_tracks:
            return
        if len(apple_tracks) == 1:
            track_name, artist_name = apple_tracks[0]
            query = f"{track_name} {artist_name}"
            try:
                cache_key = sanitize_query(query).lower()
                ydl_opts_full = {
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "no_color": True,
                    "socket_timeout": 10,
                    "force_generic_extractor": True,
                    "cookiefile": "/mnt/server/cookies.txt"
                }
                sanitized_query = sanitize_query(query)
                search_query = f"ytsearch:{sanitized_query}"
                info = await extract_info_async(ydl_opts_full, search_query)
                video = info["entries"][0] if "entries" in info and info["entries"] else None
                if not video:
                    raise Exception("No results found")
                video_url = video.get("webpage_url", video.get("url"))
                if not video_url:
                    raise KeyError("No valid URL found in video metadata")
                logger.debug(f"Metadata for a single Apple Music track : {video}")
                url_cache[cache_key] = video_url
                await music_player.queue.put({'url': video_url, 'is_single': True, 'skip_now_playing': True})
                embed = Embed(
                    title=get_messages("song_added", guild_id),
                    description=f"[{video.get('title', track_name)}]({video_url})",
                    color=0xC7CEEA if is_kawaii else discord.Color.blue()
                )
                if video.get("thumbnail"):
                    embed.set_thumbnail(url=video["thumbnail"])
                if is_kawaii:
                    embed.set_footer(text="☆⌒(≧▽° )")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                logger.error(f"Apple Music Conversion Error for {query} : {e}")
                embed = Embed(
                    description=get_messages("search_error", guild_id),
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = Embed(
                title="Apple Music playlist processing",
                description="Starting...",
                color=0xFFB6C1 if is_kawaii else discord.Color.blue()
            )
            message = await interaction.followup.send(embed=embed)
            total_tracks = len(apple_tracks)
            processed = 0
            failed = 0
            failed_tracks = []
            batch_size = 50
            for i in range(0, total_tracks, batch_size):
                batch = apple_tracks[i:i + batch_size]
                tasks = [search_track(track) for track in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception) or result[1] is None:
                        failed += 1
                        if len(failed_tracks) < 5:
                            failed_tracks.append(f"{result[2]} par {result[3]}")
                    else:
                        _, video_url, _, _ = result
                        await music_player.queue.put({'url': video_url, 'is_single': False})
                    processed += 1
                    if processed % 10 == 0 or processed == total_tracks:
                        progress = processed / total_tracks
                        bar = create_loading_bar(progress)
                        description = f"{bar}\n" + get_messages("loading_playlist", guild_id).format(
                            processed=processed,
                            total=total_tracks
                        )
                        embed.description = description
                        await message.edit(embed=embed)
                await asyncio.sleep(0.1)
            if processed - failed == 0:
                embed = Embed(
                    description="⚠️ Apple Music Conversion Error for.",
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await message.edit(embed=embed)
                return
            failed_text = "\nFailed tracks (up to 5) :\n" + "\n".join([f"- {track}" for track in failed_tracks]) if failed_tracks else ""
            embed.title = get_messages("apple_music_playlist_added", guild_id)
            embed.description = get_messages("apple_music_playlist_description", guild_id).format(
                count=processed - failed,
                failed=failed,
                failed_tracks=failed_text
            )
            embed.color = 0xB5EAD7 if is_kawaii else discord.Color.green()
            await message.edit(embed=embed)
    elif tidal_regex.match(query):
        tidal_tracks = await process_tidal_url(query, interaction)
        if not tidal_tracks:
            return

        if len(tidal_tracks) == 1:
            track_name, artist_name = tidal_tracks[0]
            query = f"{track_name} {artist_name}"
            try:
                cache_key = sanitize_query(query).lower()
                ydl_opts_full = {
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "no_color": True,
                    "socket_timeout": 10,
                    "force_generic_extractor": True,
                    "cookiefile": "/mnt/server/cookies.txt"
                }
                sanitized_query = sanitize_query(query)
                search_query = f"ytsearch:{sanitized_query}"
                info = await extract_info_async(ydl_opts_full, search_query)
                video = info["entries"][0] if "entries" in info and info["entries"] else None
                if not video:
                    raise Exception("No results found")
                video_url = video.get("webpage_url", video.get("url"))
                if not video_url:
                    raise KeyError("No valid URL found in video metadata")
                logger.debug(f"Metadata for a single Tidal track : {video}")
                url_cache[cache_key] = video_url
                await music_player.queue.put({'url': video_url, 'is_single': True, 'skip_now_playing': True})
                embed = Embed(
                    title=get_messages("song_added", guild_id),
                    description=f"[{video.get('title', track_name)}]({video_url})",
                    color=0xC7CEEA if is_kawaii else discord.Color.blue()
                )
                if video.get("thumbnail"):
                    embed.set_thumbnail(url=video["thumbnail"])
                if is_kawaii:
                    embed.set_footer(text="☆⌒(≧▽° )")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                logger.error(f"Tidal conversion error for {query} : {e}")
                embed = Embed(
                    description=get_messages("search_error", guild_id),
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = Embed(
                title="Tidal mix/playlist processing",
                description="Starting...",
                color=0xFFB6C1 if is_kawaii else discord.Color.blue()
            )
            message = await interaction.followup.send(embed=embed)
            total_tracks = len(tidal_tracks)
            processed = 0
            failed = 0
            failed_tracks = []
            batch_size = 50
            for i in range(0, total_tracks, batch_size):
                batch = tidal_tracks[i:i + batch_size]
                tasks = [search_track(track) for track in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception) or result[1] is None:
                        failed += 1
                        if len(failed_tracks) < 5:
                            failed_tracks.append(f"{result[2]} par {result[3]}")
                    else:
                        _, video_url, _, _ = result
                        await music_player.queue.put({'url': video_url, 'is_single': False})
                    processed += 1
                    if processed % 10 == 0 or processed == total_tracks:
                        progress = processed / total_tracks
                        bar = create_loading_bar(progress)
                        description = f"{bar}\n" + get_messages("loading_playlist", guild_id).format(
                            processed=processed,
                            total=total_tracks
                        )
                        embed.description = description
                        await message.edit(embed=embed)
                await asyncio.sleep(0.1)
            if processed - failed == 0:
                embed = Embed(
                    description="⚠️ No tracks could be added.",
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await message.edit(embed=embed)
                return
            failed_text = "\nFailed tracks (up to 5) :\n" + "\n".join([f"- {track}" for track in failed_tracks]) if failed_tracks else ""
            embed.title = get_messages("tidal_playlist_added", guild_id)
            embed.description = get_messages("tidal_playlist_description", guild_id).format(
                count=processed - failed,
                failed=failed,
                failed_tracks=failed_text
            )
            embed.color = 0xB5EAD7 if is_kawaii else discord.Color.green()
            await message.edit(embed=embed)

    elif amazon_music_regex.match(query):
        logger.info(f"Attempting to process an Amazon Music URL : {query}")
        amazon_tracks = await process_amazon_music_url(query, interaction)
        if not amazon_tracks:
            logger.error(f"No Amazon Music tracks extracted for {query}")
            return

        if len(amazon_tracks) == 1:
            track_name, artist_name = amazon_tracks[0]
            query = f"{track_name} {artist_name}"
            try:
                cache_key = sanitize_query(query).lower()
                ydl_opts_full = {
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "no_color": True,
                    "socket_timeout": 10,
                    "force_generic_extractor": True,
                    "cookiefile": "/mnt/server/cookies.txt"
                }
                sanitized_query = sanitize_query(query)
                search_query = f"ytsearch:{sanitized_query}"
                info = await extract_info_async(ydl_opts_full, search_query)
                video = info["entries"][0] if "entries" in info and info["entries"] else None
                if not video:
                    raise Exception("No results found")
                video_url = video.get("webpage_url", video.get("url"))
                if not video_url:
                    raise KeyError("No valid URL found in video metadata")
                logger.debug(f"Metadata for a single Amazon Music track : {video}")
                url_cache[cache_key] = video_url
                await music_player.queue.put({'url': video_url, 'is_single': True, 'skip_now_playing': True})
                embed = Embed(
                    title=get_messages("song_added", guild_id),
                    description=f"[{video.get('title', track_name)}]({video_url})",
                    color=0xC7CEEA if is_kawaii else discord.Color.blue()
                )
                if video.get("thumbnail"):
                    embed.set_thumbnail(url=video["thumbnail"])
                if is_kawaii:
                    embed.set_footer(text="☆⌒(≧▽° )")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                logger.error(f"Amazon Music Conversion Error for {query} : {e}")
                embed = Embed(
                    description=get_messages("search_error", guild_id),
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = Embed(
                title="Amazon Music playlist processing",
                description="Starting...",
                color=0xFFB6C1 if is_kawaii else discord.Color.blue()
            )
            message = await interaction.followup.send(embed=embed)
            total_tracks = len(amazon_tracks)
            processed = 0
            failed = 0
            failed_tracks = []
            batch_size = 50
            for i in range(0, total_tracks, batch_size):
                batch = amazon_tracks[i:i + batch_size]
                tasks = [search_track(track) for track in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception) or result[1] is None:
                        failed += 1
                        if len(failed_tracks) < 5:
                            failed_tracks.append(f"{result[2]} par {result[3]}")
                    else:
                        _, video_url, _, _ = result
                        await music_player.queue.put({'url': video_url, 'is_single': False})
                    processed += 1
                    if processed % 10 == 0 or processed == total_tracks:
                        progress = processed / total_tracks
                        bar = create_loading_bar(progress)
                        description = f"{bar}\n" + get_messages("loading_playlist", guild_id).format(
                            processed=processed,
                            total=total_tracks
                        )
                        embed.description = description
                        await message.edit(embed=embed)
                await asyncio.sleep(0.1)
            if processed - failed == 0:
                embed = Embed(
                    description="⚠️ No tracks could be added.",
                    color=0xFF9AA2 if is_kawaii else discord.Color.red()
                )
                await message.edit(embed=embed)
                return
            failed_text = "\nFailed tracks (up to 5) :\n" + "\n".join([f"- {track}" for track in failed_tracks]) if failed_tracks else ""
            embed.title = get_messages("amazon_music_playlist_added", guild_id)
            embed.description = get_messages("amazon_music_playlist_description", guild_id).format(
                count=processed - failed,
                failed=failed,
                failed_tracks=failed_text
            )
            embed.color = 0xB5EAD7 if is_kawaii else discord.Color.green()
            await message.edit(embed=embed)

    elif soundcloud_regex.match(query) or youtube_regex.match(query) or ytmusic_regex.match(query) or bandcamp_regex.match(query):
        try:
            platform = ""
            if soundcloud_regex.match(query):
                platform = "SoundCloud"
            elif ytmusic_regex.match(query):
                platform = "YouTube Music"
            elif youtube_regex.match(query):
                platform = "YouTube"
            elif bandcamp_regex.match(query):
                platform = "Bandcamp"
            elif amazon_music_regex.match(query):
                platform = "Amazon Music"
                
            ydl_opts_playlist = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "extract_flat": "in_playlist",
                "noplaylist": False,
                "no_color": True,
                "socket_timeout": 10,
                "force_generic_extractor": True,
                "cookiefile": "/mnt/server/cookies.txt"
            }
            info = await extract_info_async(ydl_opts_playlist, query)
            
            if "entries" in info:
                total_tracks = len(info["entries"])
                processed = 0
                    
                embed = Embed(
                    title=f"{platform} playlist processing",
                    description=get_messages("loading_playlist", guild_id).format(processed=0, total=total_tracks),
                    color=0xFFB6C1 if is_kawaii else discord.Color.blue()
                )
                message = await interaction.followup.send(embed=embed)
                for entry in info["entries"]:
                    if entry:
                        await music_player.queue.put({'url': entry['url'], 'is_single': False})
                        processed += 1
                        
                        if processed % 10 == 0 or processed == total_tracks:
                            progress = processed / total_tracks
                            bar = create_loading_bar(progress)
                            new_description = f"{bar}\n" + get_messages("loading_playlist", guild_id).format(
                                processed=processed, 
                                total=total_tracks
                            )
                            embed.description = new_description
                            await message.edit(embed=embed)
                
                if info["entries"] and info["entries"][0]:
                    thumbnail = info["entries"][0].get("thumbnail")
                    embed.title = get_messages("ytmusic_playlist_added", guild_id) if ytmusic_regex.match(query) else get_messages("playlist_added", guild_id)
                    embed.description = get_messages("ytmusic_playlist_description", guild_id).format(count=total_tracks) if ytmusic_regex.match(query) else get_messages("playlist_description", guild_id).format(count=total_tracks)
                    embed.color = 0xE2F0CB if is_kawaii else discord.Color.green()
                    if thumbnail:
                        embed.set_thumbnail(url=thumbnail)
                    await message.edit(embed=embed)
            else:
                await music_player.queue.put({'url': info["webpage_url"], 'is_single': True, 'skip_now_playing': True})
                embed = Embed(
                    title=get_messages("song_added", guild_id),
                    description=f"[{info['title']}]({info['webpage_url']})",
                    color=0xFFDAC1 if is_kawaii else discord.Color.blue()
                )
                if info.get("thumbnail"):
                    embed.set_thumbnail(url=info["thumbnail"])
                await interaction.followup.send(embed=embed)
        except Exception as e:
            embed = Embed(
                description=get_messages("video_error", guild_id),
                color=0xFF9AA2 if is_kawaii else discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.error(f"Error: {e}")
    else:
        try:
            ydl_opts_full = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "no_color": True,
                "socket_timeout": 10,
                "force_generic_extractor": True,
                "cookiefile": "/mnt/server/cookies.txt"
            }
            sanitized_query = sanitize_query(query)
            search_query = f"ytsearch:{sanitized_query}"
            info = await extract_info_async(ydl_opts_full, search_query)
            video = info["entries"][0] if "entries" in info and info["entries"] else None
            if not video:
                raise Exception("No results found")
            video_url = video.get("webpage_url", video.get("url"))
            if not video_url:
                raise KeyError("No valid URL found in video metadata")
            
            logger.debug(f"Metadata for keyword search: {video}")
            await music_player.queue.put({'url': video_url, 'is_single': True, 'skip_now_playing': True})
            
            embed = Embed(
                title=get_messages("song_added", guild_id),
                description=f"[{video.get('title', 'Unknown Title')}]({video_url})",
                color=0xB5EAD7 if is_kawaii else discord.Color.blue()
            )
            if video.get("thumbnail"):
                embed.set_thumbnail(url=video["thumbnail"])
            if is_kawaii:
                embed.set_footer(text="☆⌒(≧▽° )")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            embed = Embed(
                description=get_messages("search_error", guild_id),
                color=0xFF9AA2 if is_kawaii else discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.error(f"Error searching for {query}: {e}")

    if not music_player.current_task or music_player.current_task.done():
        music_player.current_task = asyncio.create_task(play_audio(guild_id))

# /queue command
@bot.tree.command(name="queue", description="Show the current queue")
async def queue(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if music_player.queue.qsize() == 0:
        embed = Embed(
            description=get_messages("queue_empty", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    queue_items = list(music_player.queue._queue)[:5]
    next_songs = []
    for item in queue_items:
        try:
            ydl_opts = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "force_generic_extractor": True,
                "cookiefile": "/mnt/server/cookies.txt"
            }
            info = await extract_info_async(ydl_opts, item['url'])
            title = info.get("title", "Unknown Title")
            url = info.get("webpage_url", item['url'])
            next_songs.append(get_messages("queue_song", guild_id).format(title=title, url=url))
        except Exception as e:
            logger.error(f"Error retrieving information : {e}")
            next_songs.append("- Unknown song")
    
    embed = Embed(
        title=get_messages("queue_title", guild_id),
        description=get_messages("queue_description", guild_id).format(count=music_player.queue.qsize()),
        color=0xB5EAD7 if is_kawaii else discord.Color.blue()
    )
    if next_songs:
        embed.add_field(
            name=get_messages("queue_next", guild_id),
            value="\n".join(next_songs),
            inline=False
        )
    
    await interaction.followup.send(embed=embed)
    
# /clearqueue command
@bot.tree.command(name="clearqueue", description="Clear the current queue")
async def clear_queue(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    while not music_player.queue.empty():
        music_player.queue.get_nowait()
    
    embed = Embed(
        description=get_messages("clear_queue_success", guild_id),
        color=0xB5EAD7 if is_kawaii else discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="playnext", description="Add a song to play next")
@app_commands.describe(query="Link or title of the video/song to play next")
async def play_next(interaction: discord.Interaction, query: str):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if not interaction.user.voice or not interaction.user.voice.channel:
        embed = Embed(
            description=get_messages("no_voice_channel", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if not music_player.voice_client or not music_player.voice_client.is_connected():
        try:
            music_player.voice_client = await interaction.user.voice.channel.connect()
        except Exception as e:
            embed = Embed(
                description=get_messages("connection_error", guild_id),
                color=0xFF9AA2 if is_kawaii else discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.error(f"Error: {e}")
            return

    music_player.text_channel = interaction.channel
    await interaction.response.defer()

    spotify_regex = re.compile(r'^(https?://)?(open\.spotify\.com)/.+$')

    try:
        if spotify_regex.match(query):
            spotify_tracks = await process_spotify_url(query, interaction)
            if not spotify_tracks or len(spotify_tracks) != 1:
                raise Exception("Only single Spotify tracks are supported for /playnext")
            
            track_name, artist_name = spotify_tracks[0]
            search_query = f"ytsearch:{sanitize_query(f'{track_name} {artist_name}')}"
            ydl_opts = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "no_color": True,
                "socket_timeout": 10,
                "force_generic_extractor": True,
                "cookiefile": "/mnt/server/cookies.txt"
            }
            info = await extract_info_async(ydl_opts, search_query)
            video = info["entries"][0] if "entries" in info and info["entries"] else None
            if not video:
                raise Exception("No results found")
            video_url = video.get("webpage_url", video.get("url"))
            if not video_url:
                raise KeyError("No valid URL found in video metadata")
            
            logger.debug(f"Metadata for Spotify track: {video}")
        else:
            ydl_opts = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "noplaylist": True,
                "no_color": True,
                "socket_timeout": 10,
                "force_generic_extractor": True,
                "cookiefile": "/mnt/server/cookies.txt"
            }
            search_query = f"ytsearch:{sanitize_query(query)}" if not query.startswith(('http://', 'https://')) else query
            info = await extract_info_async(ydl_opts, search_query)
            video = info["entries"][0] if "entries" in info and info["entries"] else info
            video_url = video.get("webpage_url", video.get("url"))
            if not video_url:
                raise KeyError("No valid URL found in video metadata")
            
            logger.debug(f"Metadata for non-Spotify query: {video}")

        new_queue = asyncio.Queue()
        await new_queue.put({'url': video_url, 'is_single': True})
        
        while not music_player.queue.empty():
            item = await music_player.queue.get()
            await new_queue.put(item)
        
        music_player.queue = new_queue
        
        embed = Embed(
            title=get_messages("play_next_added", guild_id),
            description=f"[{video.get('title', 'Unknown Title')}]({video_url})",
            color=0xC7CEEA if is_kawaii else discord.Color.blue()
        )
        if video.get("thumbnail"):
            embed.set_thumbnail(url=video["thumbnail"])
        if is_kawaii:
            embed.set_footer(text="☆⌒(≧▽° )")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        embed = Embed(
            description=get_messages("search_error", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.error(f"Error processing /playnext for {query}: {e}")

# /nowplaying command
@bot.tree.command(name="nowplaying", description="Show the current song playing")
async def now_playing(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if music_player.current_info:
        title = music_player.current_info.get("title", "Unknown Title")
        url = music_player.current_info.get("webpage_url", music_player.current_url)
        thumbnail = music_player.current_info.get("thumbnail")
        
        embed = Embed(
            title=get_messages("now_playing_title", guild_id),
            description=get_messages("now_playing_description", guild_id).format(title=title, url=url),
            color=0xC7CEEA if is_kawaii else discord.Color.green()
        )
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        await interaction.response.send_message(embed=embed)
    else:
        embed = Embed(
            description=get_messages("no_song_playing", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# Playback function
async def play_audio(guild_id):
    try:
        is_kawaii = get_mode(guild_id)
        music_player = get_player(guild_id)
        
        while True:
            if music_player.queue.qsize() == 0:
                if music_player.autoplay_enabled and music_player.last_was_single and music_player.current_url:
                    if music_player.text_channel:
                        embed = Embed(
                            description=get_messages("autoplay_added", guild_id),
                            color=0xFFB6C1 if is_kawaii else discord.Color.blue()
                        )
                        await music_player.text_channel.send(embed=embed)
                    
                    if "youtube.com" in music_player.current_url or "youtu.be" in music_player.current_url:
                        mix_playlist_url = get_mix_playlist_url(music_player.current_url)
                        if mix_playlist_url:
                            ydl_opts = {
                                "format": "bestaudio/best",
                                "quiet": True,
                                "no_warnings": True,
                                "extract_flat": True,
                                "no_color": True,
                                "socket_timeout": 10,
                                "force_generic_extractor": True,
                                "cookiefile": "/mnt/server/cookies.txt"
                            }
                            try:
                                info = await extract_info_async(ydl_opts, mix_playlist_url)
                                if "entries" in info:
                                    current_video_id = get_video_id(music_player.current_url)
                                    for entry in info["entries"]:
                                        entry_video_id = get_video_id(entry["url"])
                                        if entry_video_id and entry_video_id != current_video_id:
                                            await music_player.queue.put({'url': entry["url"], 'is_single': True})
                            except Exception as e:
                                logger.error(f"YouTube Mix Error : {e}")
                    elif "soundcloud.com" in music_player.current_url:
                        track_id = get_soundcloud_track_id(music_player.current_url)
                        if track_id:
                            station_url = get_soundcloud_station_url(track_id)
                            if station_url:
                                ydl_opts = {
                                    "format": "bestaudio/best",
                                    "quiet": True,
                                    "no_warnings": True,
                                    "extract_flat": True,
                                    "no_color": True,
                                    "socket_timeout": 10,
                                    "force_generic_extractor": True,
                                    "cookiefile": "/mnt/server/cookies.txt"
                                }
                                try:
                                    info = await extract_info_async(ydl_opts, station_url)
                                    if "entries" in info:
                                        current_track_id = track_id
                                        for entry in info["entries"]:
                                            entry_track_id = get_soundcloud_track_id(entry["url"])
                                            if entry_track_id and entry_track_id != current_track_id:
                                                await music_player.queue.put({'url': entry["url"], 'is_single': True})
                                except Exception as e:
                                    logger.error(f"SoundCloud Station error : {e}")
                else:
                    music_player.current_task = None
                    music_player.current_info = None
                    break

            track_info = await music_player.queue.get()
            video_url = track_info['url']
            is_single = track_info['is_single']
            skip_now_playing = track_info.get('skip_now_playing', False)
            
            if music_player.queue.qsize() == 0:
                music_player.last_was_single = is_single
            else:
                music_player.last_was_single = False
            
            music_player.current_url = video_url
            try:
                if not music_player.voice_client or not music_player.voice_client.is_connected():
                    if music_player.text_channel:
                        await music_player.text_channel.guild.voice_client.disconnect()
                        music_player.voice_client = await music_player.text_channel.guild.voice_channels[0].connect()

                ydl_opts = {
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                    "no_color": True,
                    "socket_timeout": 10,
                    "force_generic_extractor": True,
                    "cookiefile": "/mnt/server/cookies.txt"
                }
                info = await extract_info_async(ydl_opts, video_url)
                music_player.current_info = info
                audio_url = info["url"]
                title = info.get("title", "Unknown Title")
                thumbnail = info.get("thumbnail")
                webpage_url = info.get("webpage_url", video_url)

                if music_player.text_channel and not skip_now_playing:
                    embed = Embed(
                        title=get_messages("now_playing_title", guild_id),
                        description=get_messages("now_playing_description", guild_id).format(
                            title=title,
                            url=webpage_url
                        ),
                        color=0xC7CEEA if is_kawaii else discord.Color.green()
                    )
                    if thumbnail:
                        embed.set_thumbnail(url=thumbnail)
                    await music_player.text_channel.send(embed=embed)

                ffmpeg_options = {
                    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    "options": "-vn",
                }
                music_player.voice_client.play(
                    discord.FFmpegPCMAudio(audio_url, **ffmpeg_options),
                    after=lambda e: logger.error(f"Error : {e}") if e else None
                )

                while music_player.voice_client.is_playing() or music_player.voice_client.is_paused():
                    await asyncio.sleep(1)

                if music_player.loop_current:
                    await music_player.queue.put({'url': video_url, 'is_single': is_single, 'skip_now_playing': skip_now_playing})
                    continue

            except Exception as e:
                logger.error(f"Audio playback error for {video_url}: {e}")
                continue

    except Exception as e:
        logger.error(f"Error in play_audio for guild {guild_id}: {e}")

# /pause command
@bot.tree.command(name="pause", description="Pause the current playback")
async def pause(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if music_player.voice_client and music_player.voice_client.is_playing():
        music_player.voice_client.pause()
        embed = Embed(
            description=get_messages("pause", guild_id),
            color=0xFFB7B2 if is_kawaii else discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed)
    else:
        embed = Embed(
            description=get_messages("no_playback", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# /resume command
@bot.tree.command(name="resume", description="Resume the playback")
async def resume(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if music_player.voice_client and music_player.voice_client.is_paused():
        music_player.voice_client.resume()
        embed = Embed(
            description=get_messages("resume", guild_id),
            color=0xB5EAD7 if is_kawaii else discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
    else:
        embed = Embed(
            description=get_messages("no_paused", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# /skip command
@bot.tree.command(name="skip", description="Skip to the next song")
async def skip(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if music_player.voice_client and music_player.voice_client.is_playing():
        music_player.voice_client.stop()
        embed = Embed(
            description=get_messages("skip", guild_id),
            color=0xE2F0CB if is_kawaii else discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed)
    else:
        embed = Embed(
            description=get_messages("no_song", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# /loop command
@bot.tree.command(name="loop", description="Enable/disable looping")
async def loop(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    music_player.loop_current = not music_player.loop_current
    state = get_messages("loop_state_enabled", guild_id) if music_player.loop_current else get_messages("loop_state_disabled", guild_id)
    
    embed = Embed(
        description=get_messages("loop", guild_id).format(state=state),
        color=0xC7CEEA if is_kawaii else discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)

# /stop command
@bot.tree.command(name="stop", description="Stop playback and disconnect the bot")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if music_player.voice_client:
        if music_player.voice_client.is_playing():
            music_player.voice_client.stop()
        
        while not music_player.queue.empty():
            music_player.queue.get_nowait()

        await music_player.voice_client.disconnect()
        music_player.voice_client = None
        music_player.current_task = None
        music_player.current_url = None
        music_player.current_info = None

        embed = Embed(
            description=get_messages("stop", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)
    else:
        embed = Embed(
            description=get_messages("not_connected", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# /shuffle command
@bot.tree.command(name="shuffle", description="Shuffle the current queue")
async def shuffle(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    if not music_player.queue.empty():
        items = []
        while not music_player.queue.empty():
            items.append(await music_player.queue.get())
        
        random.shuffle(items)
        
        music_player.queue = asyncio.Queue()
        for item in items:
            await music_player.queue.put(item)
        
        embed = Embed(
            description=get_messages("shuffle_success", guild_id),
            color=0xB5EAD7 if is_kawaii else discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
    else:
        embed = Embed(
            description=get_messages("queue_empty", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# /autoplay command
@bot.tree.command(name="autoplay", description="Enable/disable autoplay of similar songs")
async def toggle_autoplay(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    is_kawaii = get_mode(guild_id)
    music_player = get_player(guild_id)
    
    music_player.autoplay_enabled = not music_player.autoplay_enabled
    state = get_messages("autoplay_state_enabled", guild_id) if music_player.autoplay_enabled else get_messages("autoplay_state_disabled", guild_id)
    
    embed = Embed(
        description=get_messages("autoplay_toggle", guild_id).format(state=state),
        color=0xC7CEEA if is_kawaii else discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)

@bot.event
async def on_ready():
    logger.info(f"{bot.user.name} is now online.")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synchronized slash commands : {len(synced)}")

        async def rotate_presence():
            while True:
                if not bot.is_ready() or bot.is_closed():
                    return
                
                statuses = [
                    ("your Deezer links 🎶", discord.ActivityType.listening),
                    ("/play [link] 🔥", discord.ActivityType.listening),
                    (f"{len(bot.guilds)} servers 🎶", discord.ActivityType.playing)
                ]
                
                for status_text, status_type in statuses:
                    try:
                        await bot.change_presence(
                            activity=discord.Activity(
                                name=status_text,
                                type=status_type
                            )
                        )
                        await asyncio.sleep(10)
                    except Exception as e:
                        logger.error(f"Status change error : {e}")
                        await asyncio.sleep(5)

        bot.loop.create_task(rotate_presence())
        
    except Exception as e:
        logger.error(f"Error synchronizing commands : {e}")

# Run the bot (replace with your own token)
bot.run("TOKEN")
