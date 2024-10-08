from fasthtml.common import *
import env_vars

rules = (Div(f"Every question that you see is generated by AI. Every {env_vars.WORD_COUNTDOWN_SEC} seconds a new question will appear on your screen and you have to answer correctly in order to accumulate points. You get more points if more users answer correctly after you (this incentivises users to play with their friends).", style="padding: 10px; margin-top: 30px;"),
             Div("Using your points, you can bid on a new topic of your choice to appear in the future. The more points you bid the faster the topic will be shown. This means that if you bid a topic for 10 points and someone else for 5, yours will be shown first.", style="padding: 10px;"),
             Div(Div("A topic card can have one of the following statuses, depending on its current state:", style="padding: 10px;"), Ul(
                 Li("pending - This is the initial status a topic card has. When a pending card is picked up, it's first sent to a LLM (large language model) in order to confirm the topic meets quality criterias (ex: it needs to be in english, it doesn't have to have sensitive content etc.). If the LLM confirms that the proposed topic is ok, the status of the card will become 'computing'. Otherwise, it becomes 'failed'."),
                 Li("computing - Once a topic card has computing status, it's sent to an LLM to generate a trivia question and possible answers given the received topic. This process can take few seconds. When it finishes, we'll have status successful if all is ok or status failed, if the LLM failed to generate the question for some reason."),
                 Li("failed - The card failed for some reason (either technical or the user proposed a topic that is not ok)"),
                 Li("successful - A topic card has status successful when it contains the LLM generated question and the options of that question.")
                 , style="padding: 10px;")
                 )
             )