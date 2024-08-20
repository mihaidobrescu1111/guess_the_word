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
import llm_req
import copy
import env_vars
import sqlite3
from datasets import load_dataset

logging.basicConfig(level=logging.DEBUG)

SIGN_IN_TEXT = """Only logged users can play. Press on either "Sign in with HuggingFace" or "Sign in with Google"."""

db_path = f'{env_vars.DB_DIRECTORY}trivia.db'
db = database(db_path)
players = db.t.players
trivias = db.t.trivias
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
    Style('.side-panel { display: flex; flex-direction: column; width: 20%; padding: 10px; flex: 1; transition: all 0.3s ease-in-out; flex-basis: 20%;}'),
    Style('.middle-panel { display: flex; flex-direction: column; flex: 1; padding: 10px; flex: 1; transition: all 0.3s ease-in-out; flex-basis: 60%;}'),
    Style('.login { margin-bottom: 10px; max-width: fit-content; margin-left: auto; margin-right: auto;}'),
    Style('.primary:active { background-color: #0056b3; }'),
    Style('.last-tab  { display: flex; align-items: center;  justify-content: center;}'),
    Style('@media (max-width: 768px) { .side-panel { display: none; } .middle-panel { display: block; flex: 1; } .trivia-question { font-size: 20px; } #login-badge { width: 70%; } .login { display: flex; justify-content: center; align-items: center; height: 100%; } .login a {display: flex; justify-content: center; align-items: center; } #google { display: flex; justify-content: center; align-items: center; }}'),
    Style('@media (min-width: 769px) { .login_wrapper { display: none; } .bid_wrapper {display: none; } .past_topic_wrapper {display: none;} .trivia-question { font-size: 30px; }}'),
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

@dataclass
class Question:
    trivia_question: str
    option_A: str
    option_B: str
    option_C: str
    option_D: str
    correct_answer: str


@dataclass(order=True)
class Topic:
    points: int
    topic: str = field(compare=False)
    status: str = field(default="pending", compare=False)
    user: str = field(default="[bot]", compare=False)
    answers: List[Tuple[str, str]] = field(default_factory=list, compare=False)
    winners: List[str] = field(default_factory=list, compare=False)
    question: Question = field(default=None, compare=False)
    is_from_db: bool = field(default=False, compare=False)
    def __hash__(self):
        return hash((self.points, self.topic, self.user))

    def __eq__(self, other):
        if isinstance(other, Topic):
            return (self.points, self.topic, self.user) == (other.points, other.topic, other.user)
        return False


class TaskManager:
    def __init__(self, num_executors: int):
        self.topics = deque()
        self.topics_lock = asyncio.Lock()
        self.answers_lock = asyncio.Lock()
        self.past_topic = None
        self.executors = [concurrent.futures.ThreadPoolExecutor(max_workers=1) for _ in range(num_executors)]
        self.executor_tasks = [set() for _ in range(num_executors)]
        self.current_topic_start_time = None
        self.current_timeout_task = None
        self.online_users = {"unassigned_clients": {'ws_clients': set(), 'combo_count': 0 }}  # Track connected WebSocket clients
        self.online_users_lock = threading.Lock()
        self.task = None
        self.countdown_var = env_vars.QUESTION_COUNTDOWN_SEC
        self.current_topic = None
        self.all_users = {}
        self.guesses = []
        self.guesses_lock = asyncio.Lock()
        self.current_word = None

    def reset(self):
        self.countdown_var = env_vars.QUESTION_COUNTDOWN_SEC

    async def run_executor(self, executor_id: int):
        while True:
            topic_to_process = None
            async with self.topics_lock:
                for topic in self.topics:
                    if all(topic not in tasks for tasks in self.executor_tasks):
                        if topic.status not in ["successful", "failed"]:
                            self.executor_tasks[executor_id].add(topic)
                            topic_to_process = topic
                            break

            if topic_to_process:
                await self.update_status(topic_to_process)
                async with self.topics_lock:
                    self.executor_tasks[executor_id].remove(topic_to_process)
            await asyncio.sleep(0.1)

    async def update_status(self, topic: Topic):
        await asyncio.sleep(1)
        should_consume = False 
        if topic.is_from_db:
            async with self.topics_lock:
                topic.status ="successful"
        else:
            async with self.topics_lock:
                clone_topic = copy.copy(topic)
            try:
                if clone_topic.status == "pending":
                    # llm_resp = await llm_req.topic_check(clone_topic.topic)
                    llm_resp = 'Yes'
                    if llm_resp == "No":
                        status = "computing"
                    else:
                        status = "failed"
                    async with self.topics_lock:
                        topic.status = status
                elif clone_topic.status == "computing":
                    # content = await llm_req.generate_question(clone_topic.topic)
                    content = {'trivia question': 'question',
                               'option A': 'option A',
                               'option B': 'option B',
                               'option C': 'option C',
                               'option D': 'option D',
                               'correct answer': 'option A'
                               }
                    async with self.topics_lock:
                        topic.question = Question(content["trivia question"],
                                    content["option A"],
                                    content["option B"],
                                    content["option C"],
                                    content["option D"],
                                    content["correct answer"].replace(" ", "_"))
                        topic.status = "successful"
            except Exception as e:
                error_message = str(e)
                logging.debug("llm error: " + error_message)
                async with self.topics_lock:
                    topic.status = "failed"

        await self.broadcast_next_topics()
        async with self.topics_lock:
            if topic.status == "successful" and self.current_topic is None:
                should_consume = True
            if topic.status == "failed":
                await asyncio.create_task(self.remove_failed_topic(topic))
        if should_consume:
            if self.task:
                self.task.cancel()
            self.reset()
            self.task = asyncio.create_task(self.count())
            async with self.guesses_lock:
                self.guesses = []
            await self.broadcast_guesses()
            await self.consume_successful_topic()

    async def consume_successful_topic(self):
        topic = None
        async with self.topics_lock:
            successful_topics = [t for t in self.topics if t.status == "successful"]
            if successful_topics:
                topic = successful_topics[0]
                logging.debug(f"Topic obtained: {topic.topic}")
                self.topics.remove(topic)
                self.current_topic = topic
                self.current_topic_start_time = asyncio.get_event_loop().time()
                self.current_timeout_task = asyncio.create_task(self.topic_timeout())
        query = db.q(f"SELECT * FROM {words} ORDER BY RANDOM() LIMIT 1")[0]
        # self.current_topic.question.trivia_question = query['word']
        self.current_word = Word(
            word=query['word'],
            hint1=query['hint1'],
            hint2=query['hint2'],
            hint3=query['hint3'],
            hint4=query['hint4'],
            hint5=query['hint5'],
        )
        if topic:
            logging.debug(f"We have a topic to broadcast: {topic.topic}")
            await self.broadcast_current_question()
            await self.broadcast_current_word()
            await self.broadcast_next_topics()
            await self.compute_winners()
            # await self.broadcast_past_topic()
            logging.debug(f"Topic consumed: {topic.topic}")
            logging.debug(f"Length of self.topics: {len(self.topics)}")
        return topic

    async def topic_timeout(self):
        try:
            await asyncio.sleep(env_vars.QUESTION_COUNTDOWN_SEC)
            logging.debug(f"{env_vars.QUESTION_COUNTDOWN_SEC} seconds timeout completed")
            await self.check_topic_completion()
        except asyncio.CancelledError:
            logging.debug("Timeout task cancelled")
            pass

    async def check_topic_completion(self):
        should_consume = False
        async with self.topics_lock:
            current_time = asyncio.get_event_loop().time()
            logging.debug(current_time)
            logging.debug(self.current_topic_start_time)
            if self.current_topic and (current_time - self.current_topic_start_time >= env_vars.QUESTION_COUNTDOWN_SEC - 1):
                logging.debug(f"Completing topic: {self.current_topic.topic}")
                should_consume = True
        if should_consume:
            if self.task:
                self.task.cancel()
                self.reset()
            self.task = asyncio.create_task(self.count())
            self.past_topic = self.current_topic
            async with self.guesses_lock:
                self.guesses = []
            await self.broadcast_guesses()
            await self.consume_successful_topic()

    async def remove_failed_topic(self, topic: Topic):
        await asyncio.sleep(env_vars.KEEP_FAILED_TOPIC_SEC)
        if topic in self.topics and topic.status == "failed":
            self.topics.remove(topic)
            await self.broadcast_next_topics()
        logging.debug(f"Failed topic removed: {topic.topic}")

    async def monitor_topics(self):
        while True:
            need_default_topics = False
            async with self.topics_lock:
                if all(topic.status in ["successful", "failed"] for topic in self.topics):
                    need_default_topics = True

            if need_default_topics:
                await self.add_database_topics()
            await asyncio.sleep(1)  # Check periodically
    
    async def add_database_topics(self):
        async with self.topics_lock:
            if len(self.topics) < env_vars.MAX_NR_TOPICS_FOR_ALLOW_MORE:
                try:
                    trivia_recs = db.q(f"SELECT * FROM {trivias} ORDER BY RANDOM() LIMIT {env_vars.MAX_NR_TOPICS_FOR_ALLOW_MORE}")                   
                    for trivia_rec in trivia_recs:
                        self.topics.append(
                            Topic(points=0,
                                  topic=trivia_rec["topic"],
                                  user="[bot]", 
                                  question=Question(trivia_rec["question"], trivia_rec["option_A"],  trivia_rec["option_B"], trivia_rec["option_C"], trivia_rec["option_D"], "option_{}".format(trivia_rec["correct_option"])),
                                  is_from_db=True))
                    self.topics = deque(sorted(self.topics, reverse=True))
                    await self.broadcast_next_topics()
                    logging.debug("Default topics added")
                except Exception as e:
                    error_message = str(e)
                    logging.debug("Issues when generating default topics: " + error_message)

    async def add_user_topic(self, points, topic, user_id):
        async with self.topics_lock:
            self.topics.append(Topic(points=points, topic=topic, user=user_id))
            self.topics = deque(sorted(self.topics, reverse=True))
            await self.broadcast_next_topics()
            logging.debug(f"User topic: {topic} added")

    async def broadcast_next_topics(self, client=None):
        next_topics = list(self.topics)[:env_vars.NR_TOPICS_TO_BROADCAST]
        status_dict = {
            'failed': '#dc552c',
            'pending': '#ede7dd',
            'computing': '#cfb767',
            'successful': '#77ab59'
        }
        next_topics_html = [Div(Div(f"{item.topic if item.status not in ['pending', 'failed'] else 'Topic Censored'}"),
                                Div(item.user, cls="item left"), Div(f"{item.points} pts", cls="item right"),
                                cls="card", style=f"background-color: {status_dict[item.status]}") for item in
                            next_topics]
        
        await self.send_to_clients(Div(*next_topics_html, id="next_topics"), client)

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
                        
    async def compute_winners(self):
        if self.past_topic:
            async with self.answers_lock:
                self.past_topic.winners = [a[0] for a in self.past_topic.answers if a[1] == self.past_topic.question.correct_answer]
            
            ids = ", ".join([str(self.all_users[w]) for w in self.past_topic.winners])
            db_players = db.q(f"select * from {players} where {players.c.id} in ({ids})")
            
            for db_winner in db_players:
                winner_name = db_winner['name']
                db_winner['points'] += (len(self.past_topic.winners) - self.past_topic.winners.index(winner_name)) * 10
                    
                self.online_users[winner_name]['combo_count'] += 1
                if self.online_users[winner_name]['combo_count'] == env_vars.COMBO_CONSECUTIVE_NR_FOR_WIN:
                    self.online_users[winner_name]['combo_count'] = 0
                    db_winner['points'] += env_vars.COMBO_WIN_POINTS
                    
                    msg = f"Congratulations! You have earned {env_vars.COMBO_WIN_POINTS} extra points for answering {env_vars.COMBO_CONSECUTIVE_NR_FOR_WIN} questions correctly in a row."
                    elem = Div(Div(Div(msg, cls=f"toast toast-info"), cls="toast-container"), hx_swap_oob="afterbegin:body")
                    for client in self.online_users[winner_name]['ws_clients']:
                        await self.send_to_clients(elem, client) 
                        
                players.update(db_winner)
                elem = Div(winner_name + ": " + str(db_winner['points']) + " pts", cls='login', id='login_points')
                for client in self.online_users[winner_name]['ws_clients']:
                    await self.send_to_clients(elem, client)   
            
            #if you won last question, but not this one, then sorry, it has to be consecutive, so resetting to 0
            for key, user_data in self.online_users.items():
                if key not in self.past_topic.winners:
                    user_data['combo_count'] = 0
                
                            
    async def broadcast_past_topic(self, client=None):
        if self.past_topic:                
            ans = getattr(self.past_topic.question, self.past_topic.question.correct_answer)
            past_topic_html = Div(Div(B("Previous question:"), P(self.past_topic.question.trivia_question)),
                                   Div(B("Correct answer:"), P(ans)),
                                   Div(
                                        B("Winners:"),
                                        Table(*[Tr(Td(winner), Td(f"{(len(self.past_topic.winners) - self.past_topic.winners.index(winner)) * 10} pts")) for winner in self.past_topic.winners]),
                                  ),
                                  cls="past-card")

            await self.send_to_clients(Div(past_topic_html, id="past_topic"), client)

    async def broadcast_current_question(self, client=None):
        current_question_info = Div(
            Div(
                Div(self.current_topic.question.trivia_question, cls="trivia-question"),
                Div(self.current_topic.user, cls="item left"),
                Div(f"{self.current_topic.points} pts", cls="item right"),
                cls="card"),
        )
        await self.send_to_clients(Div(current_question_info, id="current_question_info"), client)

    async def broadcast_current_word(self, client=None):
        current_word_info = Div(
            Div(
                Div(self.current_word.word),
                cls="card"),
        )
        await self.send_to_clients(Div(current_word_info, id="current_word_info"), client)

    async def count(self):
        self.countdown_var = env_vars.QUESTION_COUNTDOWN_SEC
        while self.countdown_var >= 0:
            await self.broadcast_countdown()
            await asyncio.sleep(1)
            self.countdown_var -= 1

    async def broadcast_countdown(self, client=None):
        countdown_format = self.countdown_var if self.countdown_var >= 10 else f"0{self.countdown_var}"
        style = "color: red;" if self.countdown_var <= 5 else ""
        countdown_div = Div(f"{countdown_format}", cls="countdown", style="text-align: center; font-size: 40px;" + style, id="countdown")
        await self.send_to_clients(countdown_div, client)

    async def broadcast_guesses(self, client=None):
        guesses = list(self.guesses)
        guesses_html = [Div(f"{elem['user_id']}: {elem['guess']}", style="border-bottom: 1px solid #ccc; padding: 5px;") for elem in guesses]

        await self.send_to_clients(
            Div(*guesses_html, id='guesses', style='height: 300px; overflow-y: auto; border: 1px solid #ccc;'),
            client
        )



def ensure_db_tables():
    if players not in db.t:
        players.create(id=int, name=str, points=int, pk='id')

    if words not in db.t:
        # bulk import from HF dataset
        dataset = load_dataset("Mihaiii/guess_the_word", split='train')
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
        logging.debug("Count trivia rows:" + str(len(list(words.rows))))
    
async def app_startup():
    ensure_db_tables()
    print()
    num_executors = 2  # Change this to run more executors
    task_manager = TaskManager(num_executors)
    app.state.task_manager = task_manager
    results = db.q(f"SELECT {players.c.name}, {players.c.id} FROM {players}")
    task_manager.all_users = {row['name']: row['id'] for row in results}
    asyncio.create_task(task_manager.monitor_topics())
    for i in range(num_executors):
        asyncio.create_task(task_manager.run_executor(i))


app = FastHTML(hdrs=(css, ThemeSwitch()), ws_hdr=True, on_startup=[app_startup])
rt = app.route
setup_toasts(app)

def guess_form():
    return Div(Form(
        Input(type='text', name='guess', placeholder="Guess the word", maxlength=f"{env_vars.TOPIC_MAX_LENGTH}",
              required=True, autofocus=True),
        Button('GUESS', cls='primary', style='width: 100%;', id="guess_btn"),
        action='/', hx_post='/guess', style='border: 5px solid #eaf6f6; padding: 10px; width: 100%; margin: 10px auto;',
        id='guess_form'), hx_swap="outerHTML"
    )


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
    A("STATS", href="/stats", role="button", cls="secondary", id="stats"),
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

    current_word_info = Div(id="current_word_info")
    left_panel = Div(
        Div(id="next_topics"),
        cls='side-panel'
    )
    
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
    
    middle_panel = Div(
        Div(top_right_corner, cls='login_wrapper'),
        Div(id="countdown"),
        current_word_info,
        Div(Div(id="past_topic"), cls='past_topic_wrapper', style='padding-top: 10px;'),
        Div(guess_form()),
        cls="middle-panel"
    )
    right_panel = Div(
        Div(top_right_corner),
        Div(id="past_topic"),
        Div(id='guesses'),
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
        A("STATS", href="/stats", role="button", cls="secondary", id="stats"),
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
    
    return Title("Trivia"), Div(container, enterToGuess())

@rt("/how-to-play")
def get(app, session):
    rules = (Div(f"Every question that you see is generated by AI. Every {env_vars.QUESTION_COUNTDOWN_SEC} seconds a new question will appear on your screen and you have to answer correctly in order to accumulate points. You get more points if more users answer correctly after you (this incentivises users to play with their friends).", style="padding: 10px; margin-top: 30px;"),
             Div("Using your points, you can bid on a new topic of your choice to appear in the future. The more points you bid the faster the topic will be shown. This means that if you bid a topic for 10 points and someone else for 5, yours will be shown first.", style="padding: 10px;"),
             Div(Div("A topic card can have one of the following statuses, depending on its current state:", style="padding: 10px;"), Ul(
                 Li("pending - This is the initial status a topic card has. When a pending card is picked up, it's first sent to a LLM (large language model) in order to confirm the topic meets quality criterias (ex: it needs to be in english, it doesn't have to have sensitive content etc.). If the LLM confirms that the proposed topic is ok, the status of the card will become 'computing'. Otherwise, it becomes 'failed'."),
                 Li("computing - Once a topic card has computing status, it's sent to an LLM to generate a trivia question and possible answers given the received topic. This process can take few seconds. When it finishes, we'll have status successful if all is ok or status failed, if the LLM failed to generate the question for some reason."),
                 Li("failed - The card failed for some reason (either technical or the user proposed a topic that is not ok)"),
                 Li("successful - A topic card has status successful when it contains the LLM generated question and the options of that question.")
                 , style="padding: 10px;")
                 )
             )
    return Title("Trivia"), Div(tabs, rules, style="font-size: 20px;", cls="container")

@rt('/stats')
async def get(session, app, request):
    task_manager = app.state.task_manager
    db_player = db.q(f"select * from {players} order by points desc limit 20")
    cells = [Tr(Td(f"{idx}.", style="padding: 5px; width: 50px; text-align: center;"), Td(row['name'], style="padding: 5px;"), Td(row['points'], style="padding: 5px; text-align: center;")) for idx, row in enumerate(db_player, start=1)]
    with task_manager.online_users_lock:
        c = [c for c in task_manager.online_users if c != "unassigned_clients"]
        
    main_content = Div(
        Div(H2("Logged in users (" + str(len(c)) + "):"), Div(", ".join(c))),
        Div(H1("Leaderboard", style="text-align: center;"), Table(Tr(Th(B("Rank")), Th(B('HuggingFace Username')), Th(B("Points"), style="text-align: center;")), *cells))
    )
    return Title("Trivia"), Div(
        tabs,
        main_content,
        cls="container"
    )

@rt('/faq')
async def get(session, app, request):
    qa = [
        ("I press the Sign in button, but nothing happens. Why?", 
        "You're probably accessing https://huggingface.co/spaces/Mihaiii/Trivia. Please use https://mihaiii-trivia.hf.space/ instead."),
        
        ("Where can I see the source code?", 
        "The files for this space can be accessed here: https://huggingface.co/spaces/Mihaiii/Trivia/tree/main. The actual source code for the Trivia game repository is available here: https://github.com/mihaiii/trivia."),
        
        ("Why do you need me to sign in? What data do you store?", 
        "We only store a very basic leaderboard table that tracks how many points each player has."),
        
        ("Is this website mobile-friendly?", 
        "Yes."),
        
        ("Where can I offer feedback?", 
        "You can contact us on X: https://x.com/m_chirculescu and https://x.com/mihaidobrescu_."),
        
        ("How is the score decided?", 
        f"The score is calculated based on the following formula: 10 + (number of people who answered correctly after you * 10). You'll receive {env_vars.COMBO_WIN_POINTS} extra points for answering correctly {env_vars.COMBO_CONSECUTIVE_NR_FOR_WIN} questions in a row."),
        
        ("If I'm not sure of an answer, should I just guess an option?", 
        "Yes. You don't lose points for answering incorrectly."),
        
        ("A trivia question had an incorrect answer. Where can I report it?", 
        "We use a language model to generate questions, and sometimes it might provide incorrect information. No need to report it. :)"),
        
        ("What languages are supported?", 
        "Ideally, we accept questions only in English, but we use a language model for checking, and it might not always work perfectly."),
        
        ("Is this safe for children?", 
        "Yes, we review the topics users submit or bid on before displaying or accepting them.")
    ]

    main_content = Ul(*[Li(Strong(pair[0]), Br(), P(pair[1])) for pair in qa], style="padding: 10px; font-size: 20px;")
    return Title("Trivia"), Div(
        tabs,
        main_content,
        cls="container"
    )


@rt("/guess")
async def post(session, guess: str):
    if 'session_id' not in session:
        add_toast(session, SIGN_IN_TEXT, "error")
        return guess_form()
    
    task_manager = app.state.task_manager

    guess = guess.strip()

    if guess.lower() == task_manager.current_word.word.lower():
        # add points to the user
        return guess_form()

    if len(guess) > env_vars.TOPIC_MAX_LENGTH:
        add_toast(session, f"The guess max length is {env_vars.TOPIC_MAX_LENGTH} characters", "error")
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

    async with task_manager.guesses_lock:
        task_manager.guesses.append(guess_dict)
        await task_manager.broadcast_guesses()
        logging.debug(f"Guess: {guess} from {db_player[0]['name']}")

    return guess_form()



async def on_connect(send, ws):
    client_key = "unassigned_clients"
    if ws.scope['session'] and ws.scope['session']['session_id']:
        client_key = ws.scope['session']['session_id']        
    task_manager = app.state.task_manager
    with task_manager.online_users_lock:
        if client_key not in task_manager.online_users:
            task_manager.online_users[client_key] = { 'ws_clients': set(), 'combo_count': 0}
        task_manager.online_users[client_key]['ws_clients'].add(send)
    await task_manager.broadcast_next_topics(send)
    if task_manager.current_topic:
        await task_manager.broadcast_current_question(send)
    if task_manager.current_word:
        await task_manager.broadcast_current_word(send)
    # await task_manager.broadcast_past_topic(send)
    await task_manager.broadcast_guesses(send)


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
