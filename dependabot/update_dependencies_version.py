from github import Github, InputGitAuthor, GithubException
import json
import os
from retry import retry
import sys
import time
import urllib.request

HTTP_REQUEST_RETRIES = 3
HTTP_REQUEST_DELAY_IN_SECONDS = 2
HTTP_REQUEST_DELAY_MULTIPLIER = 2

ORGANIZATION = "ballerina-platform"
LANG_VERSION_KEY = "ballerinaLangVersion"

LANG_VERSION_UPDATE_BRANCH = 'automated/dependency_version_update'

packageUser = os.environ["packageUser"]
packagePAT = os.environ["packagePAT"]
packageEmail = os.environ["packageEmail"]
reviewerPackagePAT = os.environ["reviewerPackagePAT"]

ENCODING = "utf-8"

OPEN = "open"
MODULES = "modules"

MODULE_NAME = "name"
MODULE_AUTO_MERGE = "auto_merge"
MODULE_CREATED_PR = "created_pr"

MODULE_STATUS = "status"
MODULE_STATUS_IN_PROGRESS = "in_progress"
MODULE_STATUS_COMPLETED = "completed"

MODULE_CONCLUSION = "conclusion"
MODULE_CONCLUSION_TIMED_OUT = "timed_out"
MODULE_CONCLUSION_PR_PENDING = "pr_build_pending"
MODULE_CONCLUSION_PR_CHECK_FAILURE = "pr_check_failure"
MODULE_CONCLUSION_PR_MERGE_FAILURE = "merge_failure"
MODULE_CONCLUSION_BUILD_PENDING = "build_pending"
MODULE_CONCLUSION_BUILD_SUCCESS = "build_success"
MODULE_CONCLUSION_BUILD_FAILURE = "build_failure"
MODULE_CONCLUSION_BUILD_RELEASED = "build_released"

COMMIT_MESSAGE_PREFIX = "[Automated] Update lang version to "
PULL_REQUEST_BODY_PREFIX = "Update ballerina lang version to `"
PULL_REQUEST_TITLE = "[Automated] Update Dependencies (Ballerina Lang : "
AUTO_MERGE_PULL_REQUEST_TITLE = "[AUTO MERGE] Update Dependencies (Ballerina Lang : "

MODULE_LIST_FILE = "dependabot/resources/extensions.json"
PROPERTIES_FILE = "gradle.properties"

SLEEP_INTERVAL = 30  # 30s
MAX_WAIT_CYCLES = 120

overrideBallerinaVersion = sys.argv[1]
autoMergePRs = sys.argv[2]

github = Github(packagePAT)
current_level_modules = []
lang_version = ""
status_completed_modules = 0


def main():
    global lang_version
    lang_version = get_lang_version()
    module_list_json = get_module_list_json()
    check_and_update_lang_version(module_list_json)


def get_lang_version():
    if overrideBallerinaVersion != '':
        return overrideBallerinaVersion
    else:
        try:
            version_string = open_url(
                "https://api.github.com/orgs/ballerina-platform/packages/maven/org.ballerinalang.jballerina-tools/versions").read()
        except Exception as e:
            print('[Error] Failed to get ballerina packages version', e)
            sys.exit(1)
        latest_version = json.loads(version_string)[0]
        return latest_version["name"]


@retry(
    urllib.error.URLError,
    tries=HTTP_REQUEST_RETRIES,
    delay=HTTP_REQUEST_DELAY_IN_SECONDS,
    backoff=HTTP_REQUEST_DELAY_MULTIPLIER
)
def open_url(url):
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github.v3+json")
    request.add_header("Authorization", "Bearer " + packagePAT)

    return urllib.request.urlopen(request)


def get_module_list_json():
    try:
        with open(MODULE_LIST_FILE) as f:
            module_list = json.load(f)

    except Exception as e:
        print("[Error] Error while loading modules list ", e)
        sys.exit(1)

    return module_list


def check_and_update_lang_version(module_list_json):
    module_details_list = module_list_json[MODULES]
    module_details_list.sort(reverse=True, key=lambda s: s['level'])
    last_level = module_details_list[0]['level']

    for i in range(last_level):
        current_level = i + 1
        global current_level_modules
        current_level_modules = list(filter(lambda s: s['level'] == current_level, module_list_json[MODULES]))

        for idx, module in enumerate(current_level_modules):
            print("[Info] Check lang dependency in module '" + module[MODULE_NAME] + "'")
            update_module(idx)

        if autoMergePRs.lower() == "true":
            wait_for_current_level_build(current_level)


def wait_for_current_level_build(level):
    print("[Info] Waiting for level '" + str(level) + "' module build.")
    total_modules = len(current_level_modules)

    wait_cycles = 0
    global status_completed_modules
    status_completed_modules = 0

    while status_completed_modules != total_modules:
        for idx in range(len(current_level_modules)):
            if current_level_modules[idx][MODULE_STATUS] == MODULE_STATUS_IN_PROGRESS:
                if current_level_modules[idx][MODULE_CONCLUSION] == MODULE_CONCLUSION_PR_PENDING:
                    check_pending_pr_checks(idx)
                else:
                    # Build checks test
                    check_pending_build_checks(idx)

        if wait_cycles < MAX_WAIT_CYCLES:
            time.sleep(SLEEP_INTERVAL)
            wait_cycles = wait_cycles + 1
        else:
            # Force stop script with all in progress modules printed
            print("Dependency bump script timed out. Following modules are in pending state")
            for module in current_level_modules:
                if module[MODULE_STATUS] == MODULE_STATUS_IN_PROGRESS:
                    print(module[MODULE_NAME])
            sys.exit(1)

    module_release_failure = False
    pr_checks_failed_modules = list(
        filter(lambda s: s[MODULE_CONCLUSION] == MODULE_CONCLUSION_PR_CHECK_FAILURE, current_level_modules))
    if len(pr_checks_failed_modules) != 0:
        module_release_failure = True
        print("Following modules dependency PRs have failed checks...")
        for module in pr_checks_failed_modules:
            print(module[MODULE_NAME])

    pr_merged_failed_modules = list(
        filter(lambda s: s[MODULE_CONCLUSION] == MODULE_CONCLUSION_PR_MERGE_FAILURE, current_level_modules))
    if len(pr_merged_failed_modules) != 0:
        module_release_failure = True
        print("Following modules dependency PRs could not be merged...")
        for module in pr_merged_failed_modules:
            print(module[MODULE_NAME])

    build_checks_failed_modules = list(
        filter(lambda s: s[MODULE_CONCLUSION] == MODULE_CONCLUSION_BUILD_FAILURE, current_level_modules))
    if len(build_checks_failed_modules) != 0:
        module_release_failure = True
        print("Following modules timestamped build checks failed...")
        for module in build_checks_failed_modules:
            print(module[MODULE_NAME])

    if module_release_failure:
        sys.exit(1)


def check_pending_pr_checks(index):
    global status_completed_modules
    print("[Info] Checking the status of the dependency bump PR in module '" + current_level_modules[index][
        MODULE_NAME] + "'")
    passing = True
    pending = False
    repo = github.get_repo(ORGANIZATION + "/" + current_level_modules[index][MODULE_NAME])

    failed_pr_checks = []
    if current_level_modules[index][MODULE_CREATED_PR] is not None:
        pull_request = repo.get_pull(current_level_modules[index][MODULE_CREATED_PR].number)
        sha = pull_request.head.sha
        for pr_check in repo.get_commit(sha=sha).get_check_runs():
            # Ignore codecov checks temporarily due to bug
            if not pr_check.name.startswith("codecov"):
                if pr_check.status != "completed":
                    pending = True
                    break
                elif pr_check.conclusion == "success":
                    continue
                elif (current_level_modules[index][MODULE_NAME] == "module-ballerinax-jaeger" and
                        pr_check.conclusion == "skipped"):
                    continue
                else:
                    failed_pr_check = {
                        "name": pr_check.name,
                        "html_url": pr_check.html_url
                    }
                    failed_pr_checks.append(failed_pr_check)
                    passing = False
        if not pending:
            if passing:
                if current_level_modules[index][MODULE_AUTO_MERGE] & ("AUTO MERGE" in pull_request.title):
                    try:
                        pull_request.merge()
                        print("[Info] Automated version bump PR merged for module '" + current_level_modules[index][
                            MODULE_NAME] + "'. PR: " + pull_request.html_url)
                        current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_PENDING
                    except Exception as e:
                        print("[Error] Error occurred while merging dependency PR for module '" +
                              current_level_modules[index][MODULE_NAME] + "'", e)
                        current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
                        current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_PR_MERGE_FAILURE
                        status_completed_modules += 1
                else:
                    current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
                    current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_PENDING
                    status_completed_modules += 1

            else:
                current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
                current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_PR_CHECK_FAILURE
                module_name = current_level_modules[index][MODULE_NAME]
                print("[Error] Dependency bump PR checks have failed for '" + module_name + "'")
                for check in failed_pr_checks:
                    print("[" + module_name + "] PR check '" + check["name"] + "' failed for " + check[
                        "html_url"])
                status_completed_modules += 1
    else:
        # Already successful and merged
        current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
        current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_RELEASED
        status_completed_modules += 1


def check_pending_build_checks(index):
    global status_completed_modules
    print("[Info] Checking the status of the timestamped build in module '" + current_level_modules[index][
        MODULE_NAME] + "'")
    passing = True
    pending = False
    repo = github.get_repo(ORGANIZATION + "/" + current_level_modules[index][MODULE_NAME])

    failed_build_checks = []
    if current_level_modules[index][MODULE_CREATED_PR] is not None:
        pull_request = repo.get_pull(current_level_modules[index][MODULE_CREATED_PR].number)
        sha = pull_request.merge_commit_sha
        for build_check in repo.get_commit(sha=sha).get_check_runs():
            # Ignore codecov checks temporarily due to bug
            if not build_check.name.startswith("codecov"):
                if build_check.status != "completed":
                    pending = True
                    break
                elif build_check.conclusion == "success":
                    continue
                else:
                    failed_build_checks = {
                        "name": build_check.name,
                        "html_url": build_check.html_url
                    }
                    failed_build_checks.append(failed_build_checks)
                    passing = False
        if not pending:
            if passing:
                # TODO: Get the latest timestamped release
                current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
                current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_SUCCESS
                status_completed_modules += 1
            else:
                current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
                current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_FAILURE
                module_name = current_level_modules[index][MODULE_NAME]
                print("[Error] Dependency bump PR merge build checks have failed for '" + module_name + "'")
                for check in failed_build_checks:
                    print("[" + module_name + "] Build check '" + check["name"] + "' failed for " + check[
                        "html_url"])
                status_completed_modules += 1
    else:
        # Already successful and merged
        current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
        current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_RELEASED
        status_completed_modules += 1


def update_module(idx):
    repo = github.get_repo(ORGANIZATION + "/" + current_level_modules[idx][MODULE_NAME])
    properties_file = repo.get_contents(PROPERTIES_FILE)

    properties_file = properties_file.decoded_content.decode(ENCODING)
    update, updated_properties_file = get_updated_properties_file(current_level_modules[idx][MODULE_NAME],
                                                                  properties_file)
    if update:
        commit_changes(repo, updated_properties_file, current_level_modules[idx][MODULE_NAME])
        create_pull_request(idx, repo)
    else:
        current_level_modules[idx][MODULE_STATUS] = MODULE_STATUS_IN_PROGRESS
        current_level_modules[idx][MODULE_CONCLUSION] = MODULE_CONCLUSION_PR_PENDING
        current_level_modules[idx][MODULE_CREATED_PR] = None


def get_updated_properties_file(module_name, properties_file):
    updated_properties_file = ""
    update = False

    split_lang_version = lang_version.split('-')
    processed_lang_version = split_lang_version[2] + split_lang_version[3]

    for line in properties_file.splitlines():
        if line.startswith(LANG_VERSION_KEY):
            current_version = line.split("=")[-1]

            split_current_version = current_version.split('-')

            if len(split_current_version) > 3:
                processed_current_version = split_current_version[2] + split_current_version[3]

                if processed_current_version < processed_lang_version:
                    print("[Info] Updating the lang version in module: '" + module_name + "'")
                    updated_properties_file += LANG_VERSION_KEY + "=" + lang_version + "\n"
                    update = True
                else:
                    updated_properties_file += line + "\n"
            else:
                # Stable dependency & SNAPSHOT
                print("[Info] Updating the lang version in module: '" + module_name + "'")
                updated_properties_file += LANG_VERSION_KEY + "=" + lang_version + "\n"
                update = True
        else:
            updated_properties_file += line + "\n"

    if update:
        print("[Info] Update lang dependency in module '" + module_name + "'")
    return update, updated_properties_file


def commit_changes(repo, updated_file, module_name):
    author = InputGitAuthor(packageUser, packageEmail)
    base = repo.get_branch(repo.default_branch)
    branch = LANG_VERSION_UPDATE_BRANCH

    try:
        ref = f"refs/heads/" + branch
        repo.create_git_ref(ref=ref, sha=base.commit.sha)
    except:
        print("[Info] Unmerged update branch existed in module: '" + module_name + "'")
        branch = LANG_VERSION_UPDATE_BRANCH + "_update_tmp"
        ref = f"refs/heads/" + branch
        try:
            repo.create_git_ref(ref=ref, sha=base.commit.sha)
        except GithubException as e:
            print("[Info] deleting update tmp branch existed in module: '" + module_name + "'")
            if e.status == 422:  # already exist
                repo.get_git_ref("heads/" + branch).delete()
                repo.create_git_ref(ref=ref, sha=base.commit.sha)

    remote_file = repo.get_contents(PROPERTIES_FILE, ref=LANG_VERSION_UPDATE_BRANCH)
    remote_file_contents = remote_file.decoded_content.decode("utf-8")

    if remote_file_contents == updated_file:
        print("[Info] Branch with the lang version is already present.")
    else:
        current_file = repo.get_contents(PROPERTIES_FILE, ref=branch)
        update = repo.update_file(
            current_file.path,
            COMMIT_MESSAGE_PREFIX + lang_version,
            updated_file,
            current_file.sha,
            branch=branch,
            author=author
        )
        if not branch == LANG_VERSION_UPDATE_BRANCH:
            update_branch = repo.get_git_ref("heads/" + LANG_VERSION_UPDATE_BRANCH)
            update_branch.edit(update["commit"].sha, force=True)
            repo.get_git_ref("heads/" + branch).delete()


def create_pull_request(idx, repo):
    pulls = repo.get_pulls(state=OPEN, head=LANG_VERSION_UPDATE_BRANCH)
    pr_exists = False
    created_pr = ""

    sha_of_lang = lang_version.split("-")[-1]

    for pull in pulls:
        if pull.head.ref == LANG_VERSION_UPDATE_BRANCH:
            pr_exists = True
            created_pr = pull
            pull.edit(
                title=pull.title.rsplit("-", 1)[0] + "-" + sha_of_lang + ")",
                body=pull.body.rsplit("-", 1)[0] + "-" + sha_of_lang + "`"
            )
            print("[Info] Automated version bump PR found for module '" + current_level_modules[idx][MODULE_NAME] +
                  "'. PR: " + pull.html_url)
            break

    if not pr_exists:
        try:
            pull_request_title = PULL_REQUEST_TITLE
            if (autoMergePRs.lower() == "true") & current_level_modules[idx][MODULE_AUTO_MERGE]:
                pull_request_title = AUTO_MERGE_PULL_REQUEST_TITLE
            pull_request_title = pull_request_title + lang_version + ")"

            created_pr = repo.create_pull(
                title=pull_request_title,
                body=PULL_REQUEST_BODY_PREFIX + lang_version + "`",
                head=LANG_VERSION_UPDATE_BRANCH,
                base=repo.default_branch
            )
            print("[Info] Automated version bump PR created for module '" + current_level_modules[idx][
                MODULE_NAME] + "'. PR: " + created_pr.html_url)
        except Exception as e:
            print("[Error] Error occurred while creating pull request for module '" + current_level_modules[idx][
                MODULE_NAME] + "'.", e)
            sys.exit(1)

        if (autoMergePRs.lower() == "true") & current_level_modules[idx][MODULE_AUTO_MERGE]:

            # To stop intermittent failures due to API sync
            time.sleep(5)

            r_github = Github(reviewerPackagePAT)
            repo = r_github.get_repo(ORGANIZATION + "/" + current_level_modules[idx][MODULE_NAME])
            pr = repo.get_pull(created_pr.number)
            try:
                pr.create_review(event="APPROVE")
                print("[Info] Automated version bump PR approved for module '" + current_level_modules[idx][
                    MODULE_NAME] + "'. PR: " + pr.html_url)
            except Exception as e:
                print("[Error] Error occurred while approving dependency PR for module '" + current_level_modules[idx][
                    MODULE_NAME] + "'",
                      e)
                sys.exit(1)

    current_level_modules[idx][MODULE_CREATED_PR] = created_pr
    current_level_modules[idx][MODULE_STATUS] = MODULE_STATUS_IN_PROGRESS
    current_level_modules[idx][MODULE_CONCLUSION] = MODULE_CONCLUSION_PR_PENDING


main()
