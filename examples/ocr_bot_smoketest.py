"""Temporary smoke test for the juya-review-bot code-review bot.

Safe to delete. Purpose: open a PR with this file, comment `/ocr review`
on the PR, confirm the bot posts line-level review comments, then close
the PR and delete the branch. This module is never imported by the app.

It intentionally contains a few common, easy-to-spot defects so the bot
has something concrete to flag.
"""


def read_third_field(path):
    # Defect 1: file handle is never closed (resource leak).
    # Defect 2: index [3] without checking the split length (IndexError risk).
    f = open(path)
    data = f.read()
    return data.split(",")[3]


def get_user_display_name(user):
    # Defect 3: possible None dereference if user or user.profile is None.
    return user.profile.name


def build_query(user_input):
    # Defect 4: SQL built via string concatenation (injection risk).
    return "SELECT * FROM users WHERE name = '" + user_input + "'"
