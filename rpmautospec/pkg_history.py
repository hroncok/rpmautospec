import datetime as dt
import logging
import re
import subprocess
from collections import defaultdict
from fnmatch import fnmatchcase
from functools import lru_cache, reduce
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import TextWrapper
from typing import Any, Dict, Optional, Sequence, Union

import pygit2

from .misc import AUTORELEASE_MACRO


log = logging.getLogger(__name__)


class PkgHistoryProcessor:

    changelog_ignore_patterns = [
        ".gitignore",
        # no need to ignore "changelog" explicitly
        "gating.yaml",
        "sources",
        "tests/*",
    ]

    autorelease_flags_re = re.compile(
        r"^E(?P<extraver>[^_]*)_S(?P<snapinfo>[^_]*)_P(?P<prerelease>[01])_B(?P<base>\d*)$"
    )

    def __init__(self, spec_or_path: Union[str, Path]):
        if isinstance(spec_or_path, str):
            spec_or_path = Path(spec_or_path)

        spec_or_path = spec_or_path.absolute()

        if not spec_or_path.exists():
            raise RuntimeError(f"Spec file or path '{spec_or_path}' doesn't exist.")
        elif spec_or_path.is_dir():
            self.path = spec_or_path
            self.name = spec_or_path.name
            self.specfile = spec_or_path / f"{self.name}.spec"
        elif spec_or_path.is_file():
            if spec_or_path.suffix != ".spec":
                raise ValueError(
                    "File specified as `spec_or_path` must have '.spec' as an extension."
                )
            self.path = spec_or_path.parent
            self.name = spec_or_path.stem
            self.specfile = spec_or_path
        else:
            raise RuntimeError("File specified as `spec_or_path` is not a regular file.")

        if not self.specfile.exists():
            raise RuntimeError(f"Spec file '{self.specfile}' doesn't exist in '{self.path}'.")

        try:
            if hasattr(pygit2, "GIT_REPOSITORY_OPEN_NO_SEARCH"):
                kwargs = {"flags": pygit2.GIT_REPOSITORY_OPEN_NO_SEARCH}
            else:
                # pygit2 < 1.4.0
                kwargs = {}
            # pygit2 < 1.2.0 can't cope with pathlib.Path objects
            self.repo = pygit2.Repository(str(self.path), **kwargs)
        except pygit2.GitError:
            self.repo = None

    @classmethod
    def _get_rpmverflags(cls, path: str, name: Optional[str] = None) -> Optional[str]:
        """Retrieve the epoch/version and %autorelease flags set in spec file.

        Returns None if an error is encountered.
        """
        path = Path(path)

        if not name:
            name = path.name

        specfile = path / f"{name}.spec"

        if not specfile.exists():
            return None

        query = "%|epoch?{%{epoch}:}:{}|%{version}\n%{release}\n"

        autorelease_definition = (
            AUTORELEASE_MACRO + " E%{?-e*}_S%{?-s*}_P%{?-p:1}%{!?-p:0}_B%{?-b*}"
        )

        rpm_cmd = [
            "rpm",
            "--define",
            "_invalid_encoding_terminates_build 0",
            "--define",
            autorelease_definition,
            "--define",
            "autochangelog %nil",
            "--qf",
            query,
            "--specfile",
            f"{name}.spec",
        ]

        try:
            output = (
                subprocess.check_output(rpm_cmd, cwd=path, stderr=subprocess.PIPE)
                .decode("UTF-8")
                .strip()
            )
        except Exception:
            return None

        split_output = output.split("\n")
        epoch_version = split_output[0]
        info = split_output[1]

        match = cls.autorelease_flags_re.match(info)
        if match:
            extraver = match.group("extraver") or None
            snapinfo = match.group("snapinfo") or None
            prerelease = match.group("prerelease") == "1"
            base = match.group("base")
            if base:
                base = int(base)
            else:
                base = 1
        else:
            extraver = snapinfo = prerelease = base = None

        result = {
            "epoch-version": epoch_version,
            "extraver": extraver,
            "snapinfo": snapinfo,
            "prerelease": prerelease,
            "base": base,
        }

        return result

    @lru_cache(maxsize=None)
    def _get_rpmverflags_for_commit(self, commit):
        with TemporaryDirectory(prefix="rpmautospec-") as workdir:
            try:
                specblob = commit.tree[self.specfile.name]
            except KeyError:
                # no spec file
                return None

            specpath = Path(workdir) / self.specfile.name
            with specpath.open("wb") as specfile:
                specfile.write(specblob.data)

            return self._get_rpmverflags(workdir, self.name)

    def release_number_visitor(self, commit: pygit2.Commit, children_must_continue: bool):
        """Visit a commit to determine its release number.

        The coroutine returned first determines if the parent chain(s) must be
        followed, i.e. if one parent has the same package epoch-version,
        suspends execution and yields that to the caller (usually the walk()
        method), who later sends the partial results for this commit (to be
        modified) and full results of parents back (as dictionaries), resuming
        execution to process these and finally yield back the results for this
        commit.
        """
        verflags = self._get_rpmverflags_for_commit(commit)

        if verflags:
            epoch_version = verflags["epoch-version"]
            prerelease = verflags["prerelease"]
            base = verflags["base"]
            if base is None:
                base = 1
            tag_string = "".join(f".{t}" for t in (verflags["extraver"], verflags["snapinfo"]) if t)
        else:
            epoch_version = prerelease = None
            base = 1
            tag_string = ""

        if not epoch_version:
            must_continue = True
        else:
            epoch_versions_to_check = []
            for p in commit.parents:
                verflags = self._get_rpmverflags_for_commit(p)
                if verflags:
                    epoch_versions_to_check.append(verflags["epoch-version"])
            must_continue = epoch_version in epoch_versions_to_check

        # Suspend execution, yield whether caller should continue, and get back the (partial) result
        # for this commit and parent results as dictionaries on resume.
        commit_result, parent_results = yield must_continue

        commit_result["epoch-version"] = epoch_version

        # Find the maximum applicable parent release number and increment by one.
        commit_result["release-number"] = release_number = (
            max(
                (
                    res["release-number"] if res and epoch_version == res["epoch-version"] else 0
                    for res in parent_results
                ),
                default=0,
            )
            + 1
        )

        prerel_str = "0." if prerelease else ""
        release_number_with_base = release_number + base - 1
        commit_result["release-complete"] = f"{prerel_str}{release_number_with_base}{tag_string}"

        yield commit_result

    @staticmethod
    def _files_changed_in_diff(diff: pygit2.Diff):
        files = set()
        for delta in diff.deltas:
            if delta.old_file:
                files.add(delta.old_file.path)
            if delta.new_file:
                files.add(delta.new_file.path)
        return files

    def changelog_visitor(self, commit: pygit2.Commit, children_must_continue: bool):
        """Visit a commit to generate changelog entries for it and its parents.

        It first determines if parent chain(s) must be followed, i.e. if the
        changelog file was modified in this commit or all its children and yields that to the
        caller, who later sends the partial results for this commit (to be
        modified) and full results of parents back (as dictionaries), which
        get processed and the results for this commit yielded again.
        """
        # Check if the spec file exists, if not, there will be no changelog.
        specfile_present = f"{self.name}.spec" in commit.tree

        # Find out if the changelog is different from every parent (or present, in the case of the
        # root commit).
        try:
            changelog_blob = commit.tree["changelog"]
        except KeyError:
            changelog_blob = None

        if commit.parents:
            changelog_changed = True
            for parent in commit.parents:
                try:
                    par_changelog_blob = parent.tree["changelog"]
                except KeyError:
                    par_changelog_blob = None
                if changelog_blob == par_changelog_blob:
                    changelog_changed = False
        else:
            # With root commits, changelog present means it was changed
            changelog_changed = bool(changelog_blob)

        # Establish which parent to follow (if any, and if we can).
        parent_to_follow = None
        merge_unresolvable = False
        if len(commit.parents) < 2:
            if commit.parents:
                parent_to_follow = commit.parents[0]
        else:
            for parent in commit.parents:
                if commit.tree == parent.tree:
                    # Merge done with strategy "ours" or equivalent, i.e. (at least) one parent has
                    # the same content. Follow this parent
                    parent_to_follow = parent
                    break
            else:
                # Didn't break out of loop => no parent with same tree found. If the changelog
                # is different from all parents, it was updated in the merge commit and we don't
                # care. If it didn't change, we don't know how to continue and need to flag that.
                merge_unresolvable = not changelog_changed

        commit_result, parent_results = yield (
            not (changelog_changed or merge_unresolvable)
            and specfile_present
            and children_must_continue
        )

        changelog_entry = {
            "commit-id": commit.id,
        }

        changelog_author = f"{commit.author.name} <{commit.author.email}>"
        changelog_date = dt.datetime.utcfromtimestamp(commit.commit_time).strftime("%a %b %d %Y")
        changelog_evr = f"{commit_result['epoch-version']}-{commit_result['release-complete']}"

        changelog_header = f"* {changelog_date} {changelog_author} {changelog_evr}"

        if not specfile_present:
            # no spec file => start fresh
            commit_result["changelog"] = ()
        elif merge_unresolvable:
            changelog_entry["data"] = f"{changelog_header}\n- RPMAUTOSPEC: unresolvable merge"
            changelog_entry["error"] = "unresolvable merge"
            previous_changelog = ()
            commit_result["changelog"] = (changelog_entry,)
        elif changelog_changed:
            if changelog_blob:
                changelog_entry["data"] = changelog_blob.data.decode("utf-8", errors="replace")
            else:
                # Changelog removed. Oops.
                changelog_entry[
                    "data"
                ] = f"{changelog_header}\n- RPMAUTOSPEC: changelog file removed"
                changelog_entry["error"] = "changelog file removed"
            commit_result["changelog"] = (changelog_entry,)
        else:
            # Pull previous changelog entries from parent result (if any).
            if len(commit.parents) == 1:
                previous_changelog = parent_results[0].get("changelog", ())
            else:
                previous_changelog = ()
                for candidate in parent_results:
                    if candidate["commit-id"] == parent_to_follow:
                        previous_changelog = candidate.get("changelog", ())
                        break

            # Check if this commit should be considered for the RPM changelog.
            if parent_to_follow:
                diff = parent_to_follow.tree.diff_to_tree(commit.tree)
            else:
                diff = commit.tree.diff_to_tree(swap=True)
            changed_files = self._files_changed_in_diff(diff)
            # Skip if no files changed (i.e. commit solely for changelog/build) or if any files are
            # not to be ignored.
            skip_for_changelog = changed_files and all(
                any(fnmatchcase(f, pat) for pat in self.changelog_ignore_patterns)
                for f in changed_files
            )

            if not skip_for_changelog:
                commit_subject = commit.message.split("\n")[0].strip()
                if commit_subject.startswith("-"):
                    commit_subject = commit_subject[1:].lstrip()
                if not commit_subject:
                    commit_subject = "RPMAUTOSPEC: empty commit log subject after stripping"
                    changelog_entry["error"] = "empty commit log subject"
                wrapper = TextWrapper(width=75, subsequent_indent="  ")
                wrapped_msg = wrapper.fill(f"- {commit_subject}")
                changelog_entry["data"] = f"{changelog_header}\n{wrapped_msg}"
                commit_result["changelog"] = (changelog_entry,) + previous_changelog
            else:
                commit_result["changelog"] = previous_changelog

        yield commit_result

    def _run_on_history(
        self, head: pygit2.Commit, *, visitors: Sequence = ()
    ) -> Dict[pygit2.Commit, Dict[str, Any]]:
        """Process historical commits with visitors and gather results."""
        # maps visited commits to their (in-flight) visitors and if they must
        # continue
        commit_coroutines = {}
        commit_coroutines_must_continue = {}

        # keep track of branches
        branch_heads = [head]
        branches = []

        ########################################################################################
        # Unfortunately, pygit2 only tells us what the parents of a commit are, not what other
        # commits a commit is parent to (its children). Fortunately, Repository.walk() is quick.
        ########################################################################################
        commit_children = defaultdict(list)
        for commit in self.repo.walk(head.id):
            for parent in commit.parents:
                commit_children[parent].append(commit)

        ##########################################################################################
        # To process, first walk the tree from the head commit downward, following all branches.
        # Check visitors whether they need parent results to do their work, i.e. the history needs
        # to be followed further.
        ##########################################################################################

        # While new branch heads are encountered...
        while branch_heads:
            commit = branch_heads.pop(0)
            branch = []
            branches.append(branch)

            while True:
                if commit in commit_coroutines:
                    break

                log.debug("%s: %s", commit.short_id, commit.message.split("\n")[0])

                if commit == head:
                    children_visitors_must_continue = [True for v in visitors]
                else:
                    this_children = commit_children[commit]
                    if not all(child in commit_coroutines for child in this_children):
                        # there's another branch that leads to this parent, put the remainder on the
                        # stack
                        branch_heads.append(commit)
                        if not branch:
                            # don't keep empty branches on the stack
                            branches.pop()
                        break

                    # For all visitor coroutines, determine if any of the children must continue.
                    children_visitors_must_continue = [
                        reduce(
                            lambda must_continue, child: (
                                must_continue or commit_coroutines_must_continue[child][vindex]
                            ),
                            this_children,
                            False,
                        )
                        for vindex, v in enumerate(visitors)
                    ]

                branch.append(commit)

                # Create visitor coroutines for the commit from the functions passed into this
                # method. Pass the ordered list of "is there a child whose coroutine of the same
                # visitor wants to continue" into it.
                commit_coroutines[commit] = coroutines = [
                    v(commit, children_visitors_must_continue[vi]) for vi, v in enumerate(visitors)
                ]

                # Consult all visitors for the commit on whether we should continue and store the
                # results.
                commit_coroutines_must_continue[commit] = coroutines_must_continue = [
                    next(c) for c in coroutines
                ]

                if not any(coroutines_must_continue) or not commit.parents:
                    break

                if len(commit.parents) > 1:
                    # merge commit, store new branch head(s) to follow later
                    branch_parents = commit.parents[1:]
                    new_branches = [p for p in branch_parents if p not in commit_coroutines]
                    branch_heads.extend(new_branches)

                # follow (first) parent
                commit = commit.parents[0]

        ###########################################################################################
        # Now, `branches` contains disjunct lists of commits in new -> old order. Process these in
        # reverse, one at a time until encountering a commit where we don't know the results of all
        # parents. Then put the remainder back on the stack to be further processed later until we
        # run out of branches with commits.
        ###########################################################################################

        visited_results = {}

        while branches:
            branch = branches.pop(0)
            while branch:
                # Take one commit from the tail end of the branch and process.
                commit = branch.pop()

                if not all(
                    p in visited_results or p not in commit_coroutines for p in commit.parents
                ):
                    # put the unprocessed commit back
                    branch.append(commit)
                    # put the unprocessed remainder back
                    branches.append(branch)

                    break

                parent_results = [visited_results.get(p, {}) for p in commit.parents]

                # "Pipe" the (partial) result dictionaries through the second half of all visitors
                # for the commit.
                visited_results[commit] = reduce(
                    lambda commit_result, visitor: visitor.send((commit_result, parent_results)),
                    commit_coroutines[commit],
                    {"commit-id": commit.id},
                )

        return visited_results

    def run(
        self,
        head: Optional[Union[str, pygit2.Commit]] = None,
        *,
        visitors: Sequence = (),
        all_results: bool = False,
    ) -> Union[Dict[str, Any], Dict[pygit2.Commit, Dict[str, Any]]]:
        """Process a package repository."""
        if not head:
            head = self.repo[self.repo.head.target]
        elif isinstance(head, str):
            head = self.repo[head]

        visited_results = self._run_on_history(head, visitors=visitors)

        if all_results:
            return visited_results
        else:
            return visited_results[head]
