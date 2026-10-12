"""Collaboration-file gating: PR templates, issue forms, and CODEOWNERS
appear only for the profiles that need them, and CODEOWNERS additionally
requires a known owner."""

from conftest import init_repo, run_script

PR_TEMPLATE = ".github/PULL_REQUEST_TEMPLATE.md"
CODEOWNERS = ".github/CODEOWNERS"
ISSUE_FORMS = {
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/task.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
}


def applied_files(tmp_path, overrides):
    repo = init_repo("repo-python", tmp_path, overrides=overrides)
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result
    return repo, set(result["written"])


def test_solo_trunk_gets_no_pr_ceremony(tmp_path):
    _, written = applied_files(tmp_path, [])
    assert PR_TEMPLATE not in written
    assert CODEOWNERS not in written
    assert not (ISSUE_FORMS & written)


def test_team_trunk_gets_issue_forms_but_no_pr_template(tmp_path):
    _, written = applied_files(tmp_path, ["profile=team"])
    assert ISSUE_FORMS <= written
    assert PR_TEMPLATE not in written


def test_pr_style_with_owner_gets_full_ceremony(tmp_path):
    repo, written = applied_files(tmp_path, ["workflow_style=pr", "owners.default=@KevNev19"])
    assert PR_TEMPLATE in written
    assert CODEOWNERS in written
    assert ISSUE_FORMS <= written
    assert "@KevNev19" in (repo / CODEOWNERS).read_text()


def test_pr_style_without_owner_skips_codeowners(tmp_path):
    _, written = applied_files(tmp_path, ["workflow_style=pr"])
    assert PR_TEMPLATE in written
    assert CODEOWNERS not in written, "CODEOWNERS with no owner would be broken"


def test_issue_templates_feature_flag_wins(tmp_path):
    _, written = applied_files(tmp_path, ["profile=team", "features.issue_templates=false"])
    assert not (ISSUE_FORMS & written)
