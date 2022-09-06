import os
import re

import gitlab
import requests
import yaml


class GitLabApi:
    def __init__(self, app, hook_data):
        self.app = app
        self.internal_api = self.get_internal_api()
        self.hook_data = hook_data
        self.obj_kind = self.hook_data["object_kind"]
        self.repository = self.internal_api.projects.get(
            self.hook_data["project"]["id"]
        )
        self.repository_full_name = self.hook_data["repository"]["name"]
        self.base_url = self.hook_data["repository"]["homepage"]
        self.merge_request = self.get_mr_data()
        self.verified_label = "verified"
        self.lgtm_label = "lgtm"
        self.label_by_str = "-By-"
        self.repo_mr_log_message = (
            f"{self.repository_full_name} {self.merge_request.iid}:"
        )
        self.user = self.hook_data["user"]
        self.username = self.user["username"]
        self.welcome_msg = """
The following are automatically added:
 * Add reviewers from OWNER file (in the root of the repository) under reviewers section.

Available user actions:
 * To mark MR as verified add `!verified` to a PR comment, to un-verify add `!-verified` to a MR comment.
        Verified label removed on each new commit push.
 * To approve an MR, either use the `Approve` button or add `!LGTM` or `!lgtm` to the MR comment.
 * To remove approval, either use the `Revoke approval` button or add `!-LGTM` or `!-lgtm` to the MR comment.
            """

    @staticmethod
    def get_internal_api():
        container_gitlab_config = "/python-gitlab.cfg"
        if os.path.isfile(container_gitlab_config):
            config_files = [container_gitlab_config]
        else:
            config_files = [os.path.join(os.path.expanduser("~"), "python-gitlab.cfg")]
        gitlab_api = gitlab.Gitlab.from_config(config_files=config_files)
        gitlab_api.auth()
        return gitlab_api

    def get_mr_data(self):
        if self.obj_kind == "merge_request":
            mr_id = self.hook_data["object_attributes"]["iid"]
        else:
            mr_id = self.hook_data["merge_request"]["iid"]
        self.app.logger.info(
            f"{mr_id} {self.obj_kind}: Processing... {self.hook_data['project']['name']}"
        )
        return self.repository.mergerequests.get(mr_id)

    @property
    def owners_dict(self):
        owners_file_raw_url = f"{self.base_url}/-/raw/main/OWNERS"
        resp = requests.get(owners_file_raw_url, verify="/etc/ssl/certs")
        if resp.status_code != requests.codes.ok:
            return {}
        return yaml.safe_load(
            requests.get(owners_file_raw_url, verify="/etc/ssl/certs").text
        )

    @property
    def reviewers(self):
        return self.owners_dict.get("reviewers", [])

    @property
    def approvers(self):
        return self.owners_dict.get("approvers", [])

    def label_by_user_comment(self, user_request):
        _label = user_request[1]

        # Remove label
        if user_request[0] == "-":
            self.app.logger.info(
                f"{self.repo_mr_log_message} Label removal requested by user: {_label}"
            )
            if self.lgtm_label in _label.lower():
                if self.approved_by_label in self.merge_request.labels:
                    self.add_remove_user_approve_label(action="remove")
                    if self.merge_request.approved:
                        self.merge_request.unapprove()

            else:
                self.update_merge_request(attribute_dict={"remove_labels": [_label]})
        # Add label
        else:
            self.app.logger.info(
                f"{self.repo_mr_log_message} Label addition requested by user: {_label}"
            )
            if self.lgtm_label in _label.lower():
                if self.approved_by_label not in self.merge_request.labels:
                    self.add_remove_user_approve_label(action="add")
                    self.merge_request.approve()
            else:
                self.update_merge_request(attribute_dict={"add_labels": [_label]})

    def reset_verify_label(self):
        self.app.logger.info(
            f"{self.repository_full_name}: Processing reset verify label on new commit push"
        )
        # Remove Verified label
        if self.verified_label in self.merge_request.labels:
            self.app.logger.info(f"{self.repo_mr_log_message} Removing verified label.")
            self.update_merge_request(
                attribute_dict={"remove_labels": [self.verified_label]}
            )

    def add_welcome_message(self):
        self.app.logger.info(f"{self.repo_mr_log_message} Creating welcome comment")
        self.merge_request.notes.create({"body": self.welcome_msg})

    def process_comment_webhook_data(self):
        note_body = self.hook_data["object_attributes"]["description"]
        if note_body == self.welcome_msg.rstrip():
            self.app.logger.info(
                f"{self.repo_mr_log_message} Welcome message found in comment; skipping comment processing"
            )
            return
        user_requests = re.findall(r"!(-)?(.*)", note_body)
        for user_request in user_requests:
            self.app.logger.info(
                f"{self.repo_mr_log_message} Processing label by user comment"
            )
            self.label_by_user_comment(user_request=user_request)

    def process_new_merge_request_webhook_data(self):
        # TODO: create new issue, set_label_size
        self.add_welcome_message()
        self.add_assignee()
        self.add_reviewers()

    def process_updated_merge_request_webhook_data(self):
        # TODO: Replace with bot actions
        if self.hook_data["changes"].get("labels"):
            self.app.logger.info(
                f"{self.repo_mr_log_message} No need to update the merge request, labels were updated"
            )
            return
        self.reset_verify_label()
        self.reset_reviewed_by_label()
        if self.merge_request.approvals.get().attributes["approved"]:
            self.merge_request.unapprove()

    def process_approved_merge_request_webhook_data(self):
        if [
            self.username not in label for label in self.merge_request.labels
        ] or not self.merge_request.labels:
            self.add_remove_user_approve_label(action="add")

    def process_unapproved_merge_request_webhook_data(self):
        if [self.username in label for label in self.merge_request.labels]:
            self.add_remove_user_approve_label(action="remove")

    @property
    def approved_by_label(self):
        return f"{'Approved' if self.username in self.approvers else 'Reviewed'}{self.label_by_str}{self.username}"

    def add_remove_user_approve_label(self, action):
        self.app.logger.info(
            f"{self.repo_mr_log_message} {'Add' if action == 'add' else 'Remove'} "
            f"approved label for {self.user['username']}"
        )

        if action == "add":
            self.update_merge_request(
                attribute_dict={"add_labels": [self.approved_by_label]}
            )
        else:
            self.update_merge_request(
                attribute_dict={"remove_labels": [self.approved_by_label]}
            )

    def reset_reviewed_by_label(self):
        reviewed_by_labels = [
            label
            for label in self.merge_request.labels
            if self.label_by_str.lower() in label.lower()
        ]
        if reviewed_by_labels:
            self.update_merge_request(
                attribute_dict={"remove_labels": reviewed_by_labels}
            )

    def add_assignee(self):
        self.app.logger.info(f"{self.repo_mr_log_message} Adding PR owner as assignee")
        self.update_merge_request(
            attribute_dict={"assignee_id": self.merge_request.author["id"]}
        )

    def add_reviewers(self):
        # On GitLab's free tier, it is not possible to add more than one reviewer
        reviewers_list = [
            reviewer
            for reviewer in self.reviewers
            if reviewer != self.merge_request.author["username"]
        ]
        for user in reviewers_list:
            self.app.logger.info(f"{self.repo_mr_log_message} Adding reviewer {user}")
            self.merge_request.notes.create({"body": f"@{user}"})

    def update_merge_request(self, attribute_dict):
        """
        attribute_dict: dict with merge request attribute to update
        https://docs.gitlab.com/ee/api/merge_requests.html#update-mr
        """
        self.merge_request.manager.update(self.merge_request.get_id(), attribute_dict)