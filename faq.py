from fasthtml.common import *
import env_vars

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