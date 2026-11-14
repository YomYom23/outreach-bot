Someone is filling in a short setup form for an email outreach tool. They were asked one question and
typed a reply. Decide whether the reply answers the question, or whether they are asking something back
(for example "what do you mean", "why do you need this?", "can you give an example").

What the question is for: {purpose}

Question: {question}
Their reply: <reply>{reply}</reply>

- If the reply is a usable answer, even a short or informal one, return `kind` "answer" and `reply` null.
- If it is a question, a request for help, or plainly not an answer, return `kind` "question" and put your
  answer to them in `reply`: at most three short sentences, ending with one example of a good answer.
