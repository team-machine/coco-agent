import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import srsly

import git

from . import tm_id

GIT_URL_SCHEMES = ("http", "https", "git")
GIT_SOURCE_TYPE = "git"
GIT_COMMIT_TYPE = "git_commits"
GIT_COMMIT_DIFF_TYPE = "git_commit_diffs"
GIT_REPO_TYPE = "git_repos"

log = logging.getLogger(__name__)


def get_path(objpath):
    matches = re.match(
        "^((?P<start>.*?)/?{)?(?P<a>.*) => .*?(}/(?P<end>.*))?$", objpath
    )
    if not matches:
        return objpath

    groups = ["start", "a", "end"]
    return "/".join([matches.group(g) for g in groups if matches.group(g)])


def _diff_size(diff):
    """
    Computes the size of the diff by comparing the size of the blobs.
    """
    if diff.b_blob is None and diff.deleted_file:
        # This is a deletion, so return negative the size of the original.
        return diff.a_blob.size * -1

    if diff.a_blob is None and diff.new_file:
        # This is a new file, so return the size of the new value.
        return diff.b_blob.size

    # Otherwise just return the size a-b
    return diff.a_blob.size - diff.b_blob.size


def _diff_type(diff):
    """
    Determines the type of the diff by looking at the diff flags.
    """
    if diff.renamed:
        return "R"
    if diff.deleted_file:
        return "D"
    if diff.new_file:
        return "A"
    return "M"


def clone_repo(clone_url, to_path):
    git.Repo.clone_from(clone_url, to_path)
    return git.Repo(to_path)


def get_repo_name_from_remote(repo):
    if (
        not repo.remotes
        or not hasattr(repo.remotes, "origin")
        or not repo.remotes.origin.url.endswith(".git")
    ):
        return None

    return repo.remotes.origin.url.split(".git")[0].split("/")[-1]


def repo_commits_iter(repo, rev, fallback_rev=None, reverse=True):
    """:param reverse: reverse commit order, passed by gitpython to git-rev-list reverse=False means newest to oldest"""
    try:
        for commit in repo.iter_commits(rev, reverse=reverse):
            yield commit
    except git.GitCommandError:
        repo_name = get_repo_name_from_remote(repo)
        if fallback_rev:
            log.info(f"No rev {rev} for {repo_name} - falling back to {fallback_rev}")
            yield from repo_commits_iter(repo, fallback_rev)
        else:
            log.info(
                f"Could not fetch '{rev}' for {repo_name} - "
                "assuming revision specifier does not exist"
            )


def get_repo_url_from_remote(repo, remote="origin"):
    if not repo.remotes:
        return None
    if not hasattr(repo.remotes, remote):
        return None
    return getattr(repo.remotes, remote).url


class GitRepoExtractor:
    def __init__(
        self,
        clone_url_or_path,
        sensor=None,
        customer_id=None,
        source_id=None,
        repo_tm_id=None,
        forced_repo_name=None,
        autogenerate_repo_id=False,
        repo_link_url=None,
        use_repo_link_url_from_remote=False,
    ) -> None:
        self.clone_url_or_path = clone_url_or_path
        if not sensor and (not customer_id or not source_id):
            raise ValueError(
                "Must either provide a sensor, or both customer and source ids"
            )
        if not repo_tm_id and not autogenerate_repo_id:
            raise ValueError(f"No repo id given or auto-gen requested")

        self.customer_id = sensor.customer_id if sensor else customer_id
        self.source_id = sensor.source_id if sensor else source_id
        self.sensor_id = (
            sensor.tm_id
            if sensor
            else tm_id.sensor(customer_id, GIT_SOURCE_TYPE, source_id)
        )

        self.repo_tm_id = repo_tm_id
        self.forced_repo_name = forced_repo_name
        self.autogenerate_repo_id = autogenerate_repo_id
        self.repo_link_url = repo_link_url
        self.use_repo_link_from_remote = use_repo_link_url_from_remote

    def generate_repo_id_from_remote_name(self, repo):
        repo_name = get_repo_name_from_remote(repo)
        log.info(f"Using {repo_name} for repo id from remote origin as repo id")

        return tm_id.git_repo(self.customer_id, self.source_id, repo_name)

    def load_commit_diffs(self, repo_tm_id, commit):
        """
        Source: https://bbengfort.github.io/snippets/2016/05/06/git-diff-extract.html
        This function returns a generator which iterates through all commits of
        the repository located in the given path for the given branch. It yields
        file diff information to show a timeseries of file changes.
        """
        diffs = commit.parents[0].diff(commit) if commit.parents else commit.diff()
        diffs = {
            diff.a_path: diff
            for diff in diffs
            # TODO: create_patch=True to get changed lines
        }

        # The stats on the commit is a summary of all the changes for this
        # commit, we'll iterate through it to get the information we need.
        for objpath, stats in commit.stats.files.items():
            diff = diffs.get(get_path(objpath))
            if diff is None:
                log.debug("Couldn't find a diff for %s", get_path(objpath))
                continue

            # Update the stats with the additional information
            stats.update(
                {
                    "tm_id": tm_id.git_commit_diff(commit.hexsha, objpath),
                    "sensor_id": self.sensor_id,
                    "repo_id": repo_tm_id,
                    "a_path": diff.a_path,
                    "b_path": diff.b_path,
                    "a_object_id": tm_id.git_path(repo_tm_id, diff.a_path),
                    "b_object_id": tm_id.git_path(repo_tm_id, diff.b_path),
                    "commit_id": tm_id.git_commit(commit.hexsha),
                    "size_delta": _diff_size(diff),
                    "type": _diff_type(diff),
                }
            )

            yield stats

    def extract_commits_and_history(self, repo, repo_tm_id, rev, fallback_rev=None):
        for commit_obj in repo_commits_iter(repo, rev, fallback_rev):
            diffs = self.load_commit_diffs(repo_tm_id, commit_obj)

            commit = {
                "tm_id": tm_id.git_commit(commit_obj.hexsha),
                "sensor_id": self.sensor_id,
                "repo_id": repo_tm_id,
                "diffs": list(diffs),
                "author.name": commit_obj.author.name,
                "author.email": commit_obj.author.email,
                "committer.name": commit_obj.committer.name,
                "committer.email": commit_obj.committer.email,
                "parents": [tm_id.git_commit(x.hexsha) for x in commit_obj.parents],
            }

            for attr in [
                "hexsha",
                "authored_date",
                "committed_date",
                "message",
                "summary",
            ]:
                commit[attr] = getattr(commit_obj, attr)

            yield commit

    def __call__(self, rev, fallback_rev=None):
        """Extractor for commits and diffs for a git repo. Emits 2-tuples of (rec type, record)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            if urlparse(self.clone_url_or_path).scheme in GIT_URL_SCHEMES:
                repo = clone_repo(self.clone_url_or_path, tmpdir)
            else:
                repo = git.Repo(self.clone_url_or_path)

            repo_name = self.forced_repo_name or get_repo_name_from_remote(repo)
            if not repo_name:
                raise ValueError(f"Could not infer repo name from remote")

            log.info(f"Starting extract for repo {repo_name}...")

            repo_tm_id = self.repo_tm_id
            if not repo_tm_id and self.autogenerate_repo_id:
                repo_tm_id = self.generate_repo_id_from_remote_name(repo)

            # emit commits
            for commit in self.extract_commits_and_history(
                repo, repo_tm_id, rev=rev, fallback_rev=fallback_rev
            ):
                yield GIT_COMMIT_TYPE, commit

            # emit repo
            repo_link_url = self.repo_link_url
            if not repo_link_url and self.use_repo_link_from_remote:
                repo_link_url = get_repo_url_from_remote(repo)
            yield (
                GIT_REPO_TYPE,
                {
                    "tm_id": repo_tm_id,
                    "sensor_id": self.sensor_id,
                    "name": repo_name,
                    "url": repo_link_url,
                },
            )


def ingest_and_store_repo(
    customer_id,
    source_id,
    repo_path,
    branch,
    store_fn,
    fallback_branch=None,
    forced_repo_name=None,
):
    extractor = GitRepoExtractor(
        customer_id=customer_id,
        source_id=source_id,
        clone_url_or_path=repo_path,
        autogenerate_repo_id=True,
        use_repo_link_url_from_remote=True,
        forced_repo_name=forced_repo_name,
    )

    commits, repos = [], []
    for type_, item in extractor(rev=branch, fallback_rev=fallback_branch):
        if type_ == GIT_COMMIT_TYPE:
            commits.append(item)
        elif type_ == GIT_REPO_TYPE:
            repos.append(item)
        else:
            raise ValueError(f"Unexpected item type: {type_}")

    assert len(repos) < 2, f"Did not expect more than one repo record"
    repo = repos[0]

    # sort commits first, to ease dumpoing - for now, memory usage one repo at a time isn't a concern
    commits = sorted(commits, key=lambda c: c["authored_date"])
    commit_diffs = []
    for commit in commits:
        commit_diffs.extend(commit["diffs"])
        del commit["diffs"]

    store_fn(GIT_REPO_TYPE, repo["tm_id"], repos)
    store_fn(GIT_COMMIT_TYPE, repo["tm_id"], commits)
    store_fn(GIT_COMMIT_DIFF_TYPE, repo["tm_id"], commit_diffs)

    log.info(
        f"Ingested commits for repo {repo['name']}: {len(commits)} commit(s), {len(commit_diffs)} diff(s)"
    )


def ingest_repo_to_jsonl(
    customer_id,
    source_id,
    repo_path,
    branch,
    output_dir=None,
    fall_back_from_master_to_main=True,
    forced_repo_name=None,
):
    output_dir = output_dir or os.path.join(".", "out")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    def jsonl_writer(type_, id_, iter):
        path = os.path.join(
            output_dir, f"{customer_id}__{source_id}__{id_}__{type_}.jsonl"
        )
        srsly.write_jsonl(path, iter)

    ingest_and_store_repo(
        customer_id,
        source_id,
        repo_path,
        branch,
        store_fn=jsonl_writer,
        fallback_branch=(
            "main" if branch == "master" and fall_back_from_master_to_main else None
        ),
        forced_repo_name=forced_repo_name,
    )
