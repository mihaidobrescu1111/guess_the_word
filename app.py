from fasthtml.common import *
from fasthtml.xtend import picolink
from fasthtml.oauth import GoogleAppClient
import asyncio
from collections import deque
from dataclasses import dataclass, field
import concurrent.futures
import logging
import threading
from typing import List, Tuple
from auth import HuggingFaceClient
from difflib import SequenceMatcher
from js_scripts import ThemeSwitch, enterToGuess
import env_vars
import sqlite3
from datasets import load_dataset
from how_to_play import rules
from faq import qa
import random

logging.basicConfig(level=logging.DEBUG)

SIGN_IN_TEXT = """Only logged users can play. Press on either "Sign in with HuggingFace" or "Sign in with Google"."""

db_path = f'{env_vars.DB_DIRECTORY}guess.db'
db = database(db_path)
players = db.t.players
words = db.t.words
    
def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


css = [
    picolink,
    Style('* { box-sizing: border-box; margin: 0; padding: 0; }'),
    Style('body { font-family: Arial, sans-serif; }'),
    Style('.container { display: flex; flex-direction: column; height: 100vh; }'),
    Style('.main { display: flex; flex: 1; flex-direction: row; }'),
    Style('.card { padding: 10px; margin-bottom: 10px; border: 1px solid #ccc; text-align: center; overflow: hidden;}'),
    Style('.past-card { padding: 10px; margin-bottom: 10px; border: 1px solid #ccc; text-align: left; overflow: hidden;}'),
    Style('.item { display: inline-block; }'),
    Style('.left { float: left; }'),
    Style('.right { float: right }'),
    Style('.side-panel { display: flex; flex-direction: column; width: 20%; padding: 10px; flex: 1; transition: all 0.3s ease-in-out; flex-basis: 30%;}'),
    Style('.middle-panel { display: flex; flex-direction: column; flex: 1; padding: 10px; flex: 1; transition: all 0.3s ease-in-out; flex-basis: 40%;}'),
    Style('.login { margin-bottom: 10px; max-width: fit-content; margin-left: auto; margin-right: auto;}'),
    Style('.primary:active { background-color: #0056b3; }'),
    Style('.last-tab  { display: flex; align-items: center;  justify-content: center;}'),
    Style('@media (max-width: 768px) { .side-panel { display: none; } .middle-panel { display: block; flex: 1; }  #login-badge { width: 70%; } .login { display: flex; justify-content: center; align-items: center; height: 100%; } .login a {display: flex; justify-content: center; align-items: center; } #google { display: flex; justify-content: center; align-items: center; }}'),
    Style('@media (min-width: 769px) { .login_wrapper { display: none; } }'),
    Style('@media (max-width: 446px) { #how-to-play { font-size: 12px; height: 49.6px; white-space: normal; word-wrap: break-word; display: inline-flex; justify-content: center; align-items: center} #stats { height: 49.6px; } }'),
    Style('@media (min-width: 431px) { #play { width: 152.27px; } }'),
]


huggingface_client = HuggingFaceClient(
    client_id=env_vars.HF_CLIENT_ID,
    client_secret=env_vars.HF_CLIENT_SECRET,
    redirect_uri=env_vars.HF_REDIRECT_URI
)

GoogleClient = GoogleAppClient(
    client_id=env_vars.GOOGLE_CLIENT_ID,
    redirect_uri=env_vars.GOOGLE_REDIRECT_URI,
    client_secret=env_vars.GOOGLE_CLIENT_SECRET
)

@dataclass
class Word:
    word: str
    hint1: str
    hint2: str
    hint3: str
    hint4: str
    hint5: str


class TaskManager:
    def __init__(self, num_executors: int):
        self.executors = [concurrent.futures.ThreadPoolExecutor(max_workers=1) for _ in range(num_executors)]
        self.executor_tasks = [set() for _ in range(num_executors)]
        self.current_word_start_time = None
        self.current_timeout_task = None
        self.online_users = {"unassigned_clients": {'ws_clients': set(), 'combo_count': 0, 'letters_shown': [], 'available_letters': [] }}  # Track connected WebSocket clients
        self.online_users_lock = threading.Lock()
        self.task = None
        self.countdown_var = env_vars.WORD_COUNTDOWN_SEC
        self.all_users = {}
        self.guesses = []
        self.guesses_lock = asyncio.Lock()
        self.current_word = None
        self.hints = []
        self.current_winners = []
        self.current_winners_lock = asyncio.Lock()
        self.random_letters = None
        self.current_letters = []

    def reset(self):
        self.countdown_var = env_vars.WORD_COUNTDOWN_SEC

    async def run_executor(self, executor_id: int):
        while True:
            await self.update_status()
            await asyncio.sleep(0.1)

    async def update_status(self):
        await asyncio.sleep(1)
        should_consume = False
        if self.current_word is None:
            should_consume = True
        if should_consume:
            if self.task:
                self.task.cancel()
            self.reset()
            self.task = asyncio.create_task(self.count())
            async with self.guesses_lock:
                self.guesses = []
            async with self.current_winners_lock:
                self.current_winners = []
            await self.broadcast_guesses()
            await self.consume_successful_word()
        

    async def consume_successful_word(self):
        word = None
        query = db.q(f"SELECT * FROM {words} WHERE LENGTH(word) > 5 ORDER BY RANDOM() LIMIT 1")[0]
        word = Word(
            word=query['word'],
            hint1=query['hint1'],
            hint2=query['hint2'],
            hint3=query['hint3'],
            hint4=query['hint4'],
            hint5=query['hint5'],
        )
        self.current_word = word
        self.hints = []
        self.random_letters = random.sample(range(0, len(self.current_word.word)), 2)
        with self.online_users_lock:
            for client_key in self.online_users:
                self.online_users[client_key]['letters_shown'] = []
                self.online_users[client_key]['available_letters'] = [i for i in range(len(self.current_word.word)) if i not in self.random_letters]
        self.current_word_start_time = asyncio.get_event_loop().time()
        self.current_timeout_task = asyncio.create_task(self.word_timeout())
        if word:
            logging.debug(f"We have a word to broadcast: {word.word}")
            await self.broadcast_current_word()
            await self.send_to_clients(Div(guess_form(), id='guess_form'))
            logging.debug(f"Word consumed: {word.word}")
        return word

    async def word_timeout(self):
        try:
            await asyncio.sleep(env_vars.WORD_COUNTDOWN_SEC)
            logging.debug(f"{env_vars.WORD_COUNTDOWN_SEC} seconds timeout completed")
            await self.check_word_completion()
        except asyncio.CancelledError:
            logging.debug("Timeout task cancelled")
            pass

    async def check_word_completion(self):
        should_consume = False
        current_time = asyncio.get_event_loop().time()
        logging.debug(current_time)
        logging.debug(self.current_word_start_time)
        if self.current_word and (current_time - self.current_word_start_time >= env_vars.WORD_COUNTDOWN_SEC - 1):
            logging.debug(f"Completing word: {self.current_word.word}")
            should_consume = True
        if should_consume:
            if self.task:
                self.task.cancel()
                self.reset()
            self.task = asyncio.create_task(self.count())
            async with self.guesses_lock:
                self.guesses = []
            async with self.current_winners_lock:
                self.current_winners = []
            await self.broadcast_guesses()
            await self.consume_successful_word()

    async def send_to_clients(self, element, client=None):
        with self.online_users_lock:
            clients = (self.online_users if client is None else {'unknown': { 'ws_clients': [client]}}).copy()
        for client in [item for subset in clients.values() for item in subset['ws_clients']]:
            try:
                await client(element)
            except:
                with self.online_users_lock:
                    key_to_remove = None
                    for key, clients_data in self.online_users.items():
                        if client in clients_data['ws_clients']:
                            clients_data['ws_clients'].remove(client)
                            if len(clients_data['ws_clients']) == 0:
                                key_to_remove = key
                            logging.debug(f"Removed disconnected client: {client}")
                            break
                    if key_to_remove:
                        self.online_users.pop(key_to_remove)

    async def broadcast_current_word(self, client=None):
        current_word_info = Div(
            Div(
                Div(self.current_word.word),
                cls="card"),
        )
        await self.send_to_clients(Div(current_word_info, id="current_word_info"), client)

    async def count(self):
        self.countdown_var = env_vars.WORD_COUNTDOWN_SEC
        while self.countdown_var >= 0:
            await self.broadcast_countdown()
            await self.broadcast_hints()
            await self.broadcast_letters()
            await asyncio.sleep(1)
            self.countdown_var -= 1

    async def broadcast_countdown(self, client=None):
        countdown_format = self.countdown_var if self.countdown_var >= 10 else f"0{self.countdown_var}"
        style = "color: red;" if self.countdown_var <= 5 else ""
        countdown_div = Div(f"{countdown_format}", cls="countdown", style="text-align: center; font-size: 40px;" + style, id="countdown")
        await self.send_to_clients(countdown_div, client)

    async def broadcast_guesses(self, client=None):
        guesses = list(self.guesses)
        guesses_html = [Div(
            f"{elem['user_id']}: {elem['guess']}",
            style=f"border-bottom: 1px solid #ccc; padding: 5px; background-color: {'#77ab59;' if elem['guess'] == 'answered correctly' else ''}"
        ) for elem in guesses[::-1]]
        await self.send_to_clients(Div(*guesses_html, id='guesses', style='height: 700px; overflow-y: auto; border: 1px solid #ccc; display: flex; flex-direction: column-reverse;'),client)

    async def broadcast_leaderboard(self, client=None):
        db_player = db.q(f"select * from {players} order by points desc limit 20")
        cells = [Tr(Td(f"{idx}.", style="padding: 5px; width: 50px; text-align: center;"), Td(row['name'], style="padding: 5px;"), Td(row['points'], style="padding: 5px; text-align: center;")) for idx, row in enumerate(db_player, start=1)]
            
        leaderboard = Div(
            Div(H1("Leaderboard", style="text-align: center;"), Table(Tr(Th(B("Rank")), Th(B('Username')), Th(B("Points"), style="text-align: center;")), *cells))
        )
        await self.send_to_clients(Div(leaderboard, id='leaderboard'), client)

    async def broadcast_hints(self, client=None):
        first = env_vars.WORD_COUNTDOWN_SEC / 3 * 2
        second = env_vars.WORD_COUNTDOWN_SEC / 3
        if self.current_word:
            if self.countdown_var >= first and self.current_word.hint1 not in self.hints:
                self.hints.append(self.current_word.hint1)
            if second <= self.countdown_var <= first and self.current_word.hint2 not in self.hints:
                self.hints.append(self.current_word.hint2)
            if self.countdown_var <= second and self.current_word.hint3 not in self.hints:
                self.hints.append(self.current_word.hint3)
        await self.send_to_clients(Div((Div(hint) for hint in self.hints), id='hints'), client)
    
    async def broadcast_letters(self, client=None):
        first = int(env_vars.WORD_COUNTDOWN_SEC / 4 * 3)
        second = int(env_vars.WORD_COUNTDOWN_SEC / 4 * 2)
        if self.current_word and self.countdown_var in [first, second]:
            idx = [first, second].index(self.countdown_var)
            self.current_letters.append(self.random_letters[idx])
            with self.online_users_lock:
                for client_key in self.online_users:
                    self.online_users[client_key]['letters_shown'].append(self.random_letters[idx])
        for client_key in [key for key in self.online_users]:
            word_to_show = ''.join(self.current_word.word[i] if i in self.online_users[client_key]['letters_shown'] else "_" for i in range(len(self.current_word.word)))
            for client in self.online_users[client_key]['ws_clients']:
                await self.send_to_clients(Div(word_to_show, id='hidden_word', style='font-size: 40px; letter-spacing: 10px; text-align: center;'), client)


def ensure_db_tables():
    if players not in db.t:
        players.create(id=int, name=str, points=int, pk='id')

    if words not in db.t:
        # bulk import from HF dataset
        dataset = load_dataset("Mihaiii/guess_the_word-2", split='train')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL,
                hint1 TEXT NOT NULL,
                hint2 TEXT NOT NULL,
                hint3 TEXT NOT NULL,
                hint4 TEXT NOT NULL,
                hint5 TEXT NOT NULL
            );
        ''')
        conn.commit()
        insert_query = "INSERT INTO words (word, hint1, hint2, hint3, hint4, hint5) VALUES (?, ?, ?, ?, ?, ?)"
        conn.execute('BEGIN TRANSACTION')
        for record in dataset:
            cursor.execute(insert_query, (
                record['word'], record['hint #1'], record['hint #2'], record['hint #3'], record['hint #4'],
                record['hint #5']))
        conn.commit()
        conn.close()
        logging.debug("Count word rows:" + str(len(list(words.rows))))
    
async def app_startup():
    ensure_db_tables()
    print()
    num_executors = 2  # Change this to run more executors
    task_manager = TaskManager(num_executors)
    app.state.task_manager = task_manager
    results = db.q(f"SELECT {players.c.name}, {players.c.id} FROM {players}")
    task_manager.all_users = {row['name']: row['id'] for row in results}
    for i in range(num_executors):
        asyncio.create_task(task_manager.run_executor(i))


app = FastHTML(hdrs=(css, ThemeSwitch()), ws_hdr=True, on_startup=[app_startup])
rt = app.route
setup_toasts(app)


@rt("/auth/callback")
def get(app, session, code: str = None):
    try:
        user_info = huggingface_client.retr_info(code)
    except Exception as e:
        error_message = str(e)
        return f"An error occurred: {error_message}"
    user_id = user_info.get("preferred_username")
    sub = str(user_info.get(HuggingFaceClient.id_key))
    if 'session_id' not in session:
        session['session_id'] = user_id + "#" + sub[-4:].zfill(4)
            
    logging.info(f"Client connected: {user_id}")
    return RedirectResponse(url="/")


@rt("/google/auth/callback")
def get(app, session, code: str = None):
    if not code:
        add_toast(session, "Authentication failed", "error")
        return RedirectResponse(url="/")
    GoogleClient.parse_response(code)
    user_info = GoogleClient.get_info()
    user_id = user_info.get('name')
    sub = str(user_info.get(GoogleClient.id_key))
    if 'session_id' not in session:
        session['session_id'] = user_id + "#" + sub[-4:].zfill(4)


    logging.info(f"Client connected: {user_id}")
    return RedirectResponse(url="/")


tabs = Nav(
    Div(A("PLAY", href="/", role="button", cls="secondary", id="play")),
    Div(
        A("FAQ", href="/faq", role="button", cls="secondary"),
        Div(id="theme-toggle"),
        cls="last-tab"
    ),
    cls="tabs", style="padding: 20px;"
)


@rt('/')
async def get(session, app, request):
    task_manager = app.state.task_manager

    user_id = None
    
    if 'session_id' in session:
        user_id = session['session_id']
        
        if user_id not in task_manager.all_users:
            task_manager.all_users[user_id] = None
            
        db_player = db.q(f"select * from {players} where {players.c.id} = '{task_manager.all_users[user_id]}'")
    
        if not db_player:
            current_points = 20
            players.insert({'name': user_id, 'points': current_points})
            query = f"SELECT {players.c.id} FROM {players} WHERE {players.c.name} = ?"
            result = db.q(query, (user_id,))
            task_manager.all_users[user_id] = result[0]['id']
        else:
            current_points = db_player[0]['points']


    
    if user_id:
        top_right_corner = Div(user_id + ": " + str(current_points) + " pts", cls='login', id='login_points')
    else:
        lbtn = Div(
            A(
                Img(src="https://huggingface.co/datasets/huggingface/badges/resolve/main/sign-in-with-huggingface-xl.svg", id="login-badge"), href=huggingface_client.login_link_with_state()
            )
            , cls='login')
        google_login_link = GoogleClient.prepare_request_uri(GoogleClient.base_url, GoogleClient.redirect_uri,
                                                             scope='https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile openid')
        google_btn = Div(
            A(Img(src="https://developers.google.com/identity/images/branding_guideline_sample_lt_sq_lg.svg",
                  style="width: 100%; height: auto; display: block;"), href=google_login_link), id="google")
        top_right_corner = Div(lbtn, google_btn)
    
    left_panel = Div(
        Div(id='leaderboard'),
        cls='side-panel'
    )
    middle_panel = Div(
        Div(top_right_corner, cls='login_wrapper'),
        Div(id="countdown"),
        Div(id='hidden_word'),
        Div(id="current_word_info"),
        Div(id='hints'),
        Div(id='buy_form'),
        cls="middle-panel"
    )
    right_panel = Div(
        Div(top_right_corner),
        Div(id='guesses'),
        Div(id='guess_form'),
        cls="side-panel"
    )
    main_content = Div(
        left_panel,
        middle_panel,
        right_panel,
        cls="main"
    )
    main_tabs = Nav(
        A("HOW TO PLAY?", href="/how-to-play", role="button", cls="secondary", id="how-to-play"),
        Div(
            A("FAQ", href="/faq", role="button", cls="secondary"),
            Div(id="theme-toggle"),
            cls="last-tab"
        ),
        cls="tabs", style="padding: 20px; align-items: center;"
    )
    container = Div(
        main_tabs,
        main_content,
        cls="container",
        hx_ext='ws', ws_connect='/ws'
    )
    
    return Title("Guess the word"), Div(container, enterToGuess())

@rt("/how-to-play")
def get(app, session):
    return Title("Guess the word"), Div(tabs, rules, style="font-size: 20px;", cls="container")

@rt('/faq')
async def get(session, app, request):
    main_content = Ul(*[Li(Strong(pair[0]), Br(), P(pair[1])) for pair in qa], style="padding: 10px; font-size: 20px;")
    return Title("Guess the word"), Div(
        tabs,
        main_content,
        cls="container"
    )

def guess_form(disable_var: bool = False):
    return Div(Form(
        Input(type='text', name='guess', placeholder="Guess the word", maxlength=f"{env_vars.WORD_MAX_LENGTH}",
              required=True, autofocus=True, disabled=disable_var),
        Button('GUESS', cls='primary', style='width: 100%;', id="guess_btn"),
        action='/', hx_post='/guess', style='border: 5px solid #eaf6f6; padding: 10px; width: 100%; margin: 10px auto;',
        id='guess_form'), hx_swap="outerHTML"
    )

@rt("/guess")
async def post(session, guess: str):
    if 'session_id' not in session:
        add_toast(session, SIGN_IN_TEXT, "error")
        return guess_form()
    
    task_manager = app.state.task_manager

    guess = guess.strip()

    if " " in guess:
        add_toast(session, "You can only send one word", "error")
        return guess_form()

    if len(guess) > env_vars.WORD_MAX_LENGTH:
        add_toast(session, f"The guess max length is {env_vars.WORD_MAX_LENGTH} characters", "error")
        return guess_form()

    if len(guess) == 0:
        add_toast(session, "Cannot send empty guess", "error")
        return guess_form()
    
    user_id = session['session_id']
            
    db_player = db.q(f"select * from {players} where {players.c.id} = '{task_manager.all_users[user_id]}'")

    guess_dict = {
        'guess': guess,
        'user_id': db_player[0]['name']
    }

    if guess.lower() == task_manager.current_word.word.lower():
        guess_dict['guess'] = 'answered correctly'
        db_winner = db_player[0]
        winner_name = db_winner['name']
        if winner_name in task_manager.current_winners:
            add_toast(session, "Cannot guess correctly again", "error")
            return guess_form()
        async with task_manager.current_winners_lock:
            task_manager.current_winners.append(winner_name)
        db_winner['points'] += int(50 * task_manager.countdown_var / env_vars.WORD_COUNTDOWN_SEC)
        players.update(db_winner)
        elem = Div(winner_name + ": " + str(db_winner['points']) + " pts", cls='login', id='login_points')

        for client in task_manager.online_users[winner_name]['ws_clients']:
            await task_manager.send_to_clients(elem, client)
        await task_manager.broadcast_leaderboard()
        task_manager.guesses.append(guess_dict)
        await task_manager.broadcast_guesses()
        logging.debug(f"{winner_name} guessed correctly")
        return guess_form(disable_var=True)
    else:
        if similar(guess.lower(), task_manager.current_word.word.lower()) >= 0.75:
            add_toast(session, "You're close!", "info")
        task_manager.guesses.append(guess_dict)
        await task_manager.broadcast_guesses()
        logging.debug(f"Guess: {guess} from {db_player[0]['name']}")
        return guess_form()

def buy_form():
    return Div(Form(
            Button('BUY', cls='primary', style='width: 100%;', id="buy_btn"),
            action='/', hx_post='/buy', style='border: 5px solid #eaf6f6; padding: 10px; width: 100%; margin: 10px auto;',
            id='buy_form'), hx_swap="outerHTML"
        )

@rt("/buy")
async def post(session):
    if 'session_id' not in session:
        add_toast(session, SIGN_IN_TEXT, "error")
        return buy_form()
    
    task_manager = app.state.task_manager

    user_id = session['session_id']
    if user_id in task_manager.online_users:
        try:
            if len(task_manager.online_users[user_id]['available_letters']) == 0:
                add_toast(session, "Cannot buy anymore letters", "error")
                return buy_form()
            letter = random.choice(task_manager.online_users[user_id]['available_letters'])
            task_manager.online_users[user_id]['letters_shown'].append(letter)
            task_manager.online_users[user_id]['available_letters'].remove(letter)
        except IndexError:
            add_toast(session, "Cannot buy anymore letters", "error")

    return buy_form()


async def on_connect(send, ws):
    client_key = "unassigned_clients"
    if ws.scope['session'] and ws.scope['session']['session_id']:
        client_key = ws.scope['session']['session_id']        
    task_manager = app.state.task_manager
    with task_manager.online_users_lock:
        if client_key not in task_manager.online_users:
            task_manager.online_users[client_key] = { 'ws_clients': set(), 'combo_count': 0, 'letters_shown': task_manager.current_letters, 'available_letters': list(set([i for i in range(len(task_manager.current_word.word)) if i not in task_manager.random_letters]) - set(task_manager.current_letters))}
        task_manager.online_users[client_key]['ws_clients'].add(send)
    if task_manager.current_word:
        await task_manager.broadcast_current_word(send)
    await task_manager.broadcast_guesses(send)
    await task_manager.broadcast_leaderboard(send)
    await task_manager.send_to_clients(client=send, element=Div(guess_form(), id='guess_form'))
    await task_manager.send_to_clients(client=send, element=Div(buy_form(), id='buy_form'))


async def on_disconnect(send, session):
    logging.debug("Calling on_disconnect")
    logging.debug(len(app.state.task_manager.online_users))
    task_manager = app.state.task_manager
    with task_manager.online_users_lock:
        key_to_remove = None
        for key, user_data in task_manager.online_users.items():
            if send in user_data['ws_clients']:
                user_data['ws_clients'].remove(send)
                if len(user_data['ws_clients']) == 0:
                    key_to_remove = key
                break
            
        if key_to_remove:
            task_manager.online_users.pop(key_to_remove)
                    
        if session:
            session['session_id'] = None


@app.ws('/ws', conn=on_connect, disconn=on_disconnect)
async def ws(send):
    pass
