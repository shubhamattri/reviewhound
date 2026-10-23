# Security

Please report security problems privately through GitHub's "Report a vulnerability" button on
this repository, not in a public issue.

Reviewhound reads source code and posts to GitHub with the token you give it. It never logs
tokens, redacts secrets it finds before quoting them in a comment, and reads its config from the
base branch so a pull request cannot change how it is reviewed.
