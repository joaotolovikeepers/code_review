import json
import os
from github import Github
from difflib import unified_diff
from parse_diff import parse_diff
from openai import OpenAIAPI
from fnmatch import fnmatch as minimatch

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_MODEL = os.getenv("OPENAI_API_MODEL")

g = Github(GITHUB_TOKEN)
openai_api = OpenAIAPI(api_key=OPENAI_API_KEY)


class PRDetails:
    def __init__(self, owner, repo, pull_number, title, description):
        self.owner = owner
        self.repo = repo
        self.pull_number = pull_number
        self.title = title
        self.description = description


def get_pr_details():
    event_data_path = os.getenv("GITHUB_EVENT_PATH", "")
    with open(event_data_path, "r", encoding="utf-8") as file:
        event_data = json.load(file)

    repository = event_data["repository"]
    number = event_data["number"]
    pr = g.get_repo(repository["full_name"]).get_pull(number)

    return PRDetails(
        owner=repository["owner"]["login"],
        repo=repository["name"],
        pull_number=number,
        title=pr.title,
        description=pr.body,
    )


def get_diff(owner, repo, pull_number):
    pr = g.get_repo(owner + "/" + repo).get_pull(pull_number)
    diff = pr.get_diff()

    return diff


def analyze_code(parsed_diff, pr_details):
    comments = []

    for file in parsed_diff:
        if file.to == "/dev/null":
            continue

        for chunk in file.chunks:
            prompt = create_prompt(file, chunk, pr_details)
            ai_response = get_ai_response(prompt)

            if ai_response:
                new_comments = create_comment(file, chunk, ai_response)
                if new_comments:
                    comments.extend(new_comments)

    return comments


def create_prompt(file, chunk, pr_details):
    return (
        f"Your task is to review pull requests. Instructions:\n"
        f'- Provide the response in the following JSON format: {{\'reviews\': [{{"lineNumber": <line_number>, "reviewComment": "<review comment>"}}]}}\n'
        f"- Do not give positive comments or compliments.\n"
        f'- Provide comments and suggestions ONLY if there is something to improve, otherwise "reviews" should be an empty array.\n'
        f"- Write the comment in GitHub Markdown format.\n"
        f"- Use the given description only for the overall context and only comment the code.\n"
        f"- IMPORTANT: NEVER suggest adding comments to the code.\n"
        f'Review the following code diff in the file "{file.to}" and take the pull request title and description into account when writing the response.\n'
        f"Pull request title: {pr_details.title}\n"
        f"Pull request description:\n"
        f"---\n"
        f"{pr_details.description}\n"
        f"---\n"
        f"Git diff to review:\n"
        f"```diff\n{chunk.content}\n"
        f"{''.join([f'{c.ln if c.ln else c.ln2} {c.content}' for c in chunk.changes])}\n"
        f"```\n"
    )


def get_ai_response(prompt):
    query_config = {
        "model": OPENAI_API_MODEL,
        "temperature": 0.2,
        "max_tokens": 700,
        "top_p": 1,
        "frequency_penalty": 0,
        "presence_penalty": 0,
    }

    try:
        response = openai_api.create_chat_completion(
            model=query_config["model"],
            temperature=query_config["temperature"],
            max_tokens=query_config["max_tokens"],
            top_p=query_config["top_p"],
            frequency_penalty=query_config["frequency_penalty"],
            presence_penalty=query_config["presence_penalty"],
            messages=[{"role": "system", "content": prompt}],
        )

        res = response["choices"][0]["message"]["content"].strip() or "{}"
        return json.loads(res)["reviews"]

    except Exception as error:
        print("Error:", error)
        return None


def create_comment(file, chunk, ai_responses):
    return [
        {
            "body": ai_response["reviewComment"],
            "path": file.to,
            "line": int(ai_response["lineNumber"]),
        }
        for ai_response in ai_responses
        if file.to
    ]


def create_review_comment(owner, repo, pull_number, comments):
    pr = g.get_repo(owner + "/" + repo).get_pull(pull_number)
    pr.create_review(
        event="COMMENT",
        comments=comments,
    )


def main():
    pr_details = get_pr_details()
    event_data_path = os.getenv("GITHUB_EVENT_PATH", "")

    with open(event_data_path, "r", encoding="utf-8") as file:
        event_data = json.load(file)

    if event_data["action"] == "opened":
        diff = get_diff(pr_details.owner, pr_details.repo, pr_details.pull_number)

    elif event_data["action"] == "synchronize":
        new_base_sha = event_data["before"]
        new_head_sha = event_data["after"]
        base_commit = g.get_repo(pr_details.owner + "/" + pr_details.repo).get_commit(new_base_sha)
        head_commit = g.get_repo(pr_details.owner + "/" + pr_details.repo).get_commit(new_head_sha)

        diff = "\n".join(unified_diff(base_commit.files, head_commit.files, lineterm=""))

    else:
        print("Unsupported event:", os.getenv("GITHUB_EVENT_NAME"))
        return

    if not diff:
        print("No diff found")
        return

    parsed_diff = parse_diff(diff)

    exclude_patterns = os.getenv("exclude", "").split(",")
    exclude_patterns = [pattern.strip() for pattern in exclude_patterns]

    filtered_diff = [
        file for file in parsed_diff if not any(minimatch(file.to or "", pattern) for pattern in exclude_patterns)
    ]

    comments = analyze_code(filtered_diff, pr_details)

    if comments:
        create_review_comment(pr_details.owner, pr_details.repo, pr_details.pull_number, comments)


if __name__ == "__main__":
    main()
