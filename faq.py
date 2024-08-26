from fasthtml.common import *
import env_vars

qa = [
        ("I press the Sign in button, but nothing happens. Why?", 
        "You're probably accessing https://huggingface.co/spaces/mihaidobrescu/guess-the-word. Please use https://mihaidobrescu-guess-the-word.hf.space// instead."),
        
        ("Where can I see the source code?", 
        "The source code for the game repository is available here: https://github.com/mihaidobrescu1111/guess_the_word."),
        
        ("Why do you need me to sign in? What data do you store?", 
        "We only store a very basic leaderboard table that tracks how many points each player has."),
        
        ("Where can I offer feedback?", 
        "You can contact us on X: https://x.com/m_chirculescu and https://x.com/mihaidobrescu_."),
        
        ("What languages are supported?", 
        "Ideally, we accept questions only in English, but we use a language model for checking, and it might not always work perfectly."),
        
    ]
