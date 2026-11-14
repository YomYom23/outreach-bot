You turn a user's request for sales leads into a partial run plan.

The request is about finding local {industry} companies in one US city and qualifying them against a checklist.

Return JSON with:
- `city`: the US city named in the request, as "City, ST" when the state is clear, otherwise just the city. Null if no city is named.
- `target`: the number of qualified leads the user asks for, or null if no number is given.
- `items`: one entry per condition the user wants each company to meet. Each entry has:
  - `text`: the condition as a short, positive statement about the company (for example "No AI chat or automation tool is visible on the website").
  - `code_check`: one of {code_checks} when the condition clearly means that check, otherwise null.

Only extract what the request says. Don't invent conditions, cities or numbers.

Phrases that mean each code check:
{code_check_phrases}

The request:
<request>
{prompt}
</request>
