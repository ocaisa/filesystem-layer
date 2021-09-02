#!/usr/bin/env python3

import boto3
import botocore
import github
import logging
import os
import subprocess
import tarfile


GITHUB_TOKEN_FILE = 'gh.txt'
STAGING_BUCKET = 'eessi-staging'
#STAGING_REPO = 'EESSI/staging'
STAGING_REPO = 'bedroge/eessi-staging'
METADATA_FILE_EXT = '.meta.txt'
TARBALL_DIR = '/software/tarballs'
TARBALL_INGESTION_SCRIPT = 'ingest-tarball.sh'
FAILED_INGESTION_ISSUE_BODY = '''Ingestion for tarball {tarball} has failed.

Ingestion command:
```
{command}
```

Return code:
{return_code}

Stdout:
```
{stdout}
```

Stderr:
```
{stderr}
```
'''

PR_BODY = '''A new tarball has been staged.
Please review the contents of this tarball carefully.
Merging this PR will lead to automatic ingestion of the tarball.

<details>
Directory structure inside the tarball:

```
{tar_overview}
```

</details>
'''


class EessiTarball:

    def __init__(self, tarball, github):
        self.github = github
        self.git_repo = github.get_repo(STAGING_REPO)
        self.metadata_file = tarball + METADATA_FILE_EXT
        self.tarball = tarball
        self.s3 = boto3.client('s3')
        self.local_path = None
        self.local_metadata_path = None

        self.states = {
            'new': {'handler': self.mark_new_tarball_as_staged, 'next_state': 'staged'},
            'staged': {'handler': self.make_approval_request, 'next_state': 'approved'},
            'approved': {'handler': self.ingest, 'next_state': 'ingested'},
            'ingested': {'handler': self.print_ingested},
            'rejected': {'handler': self.print_rejected},
        }

        self.state = self.find_state()


    def download(self):
        """
        Download this tarball and its corresponding metadata file, if this hasn't been already done,
        and return a tuple containing the local paths to both files.
        """
        self.local_path = os.path.join(TARBALL_DIR, os.path.basename(self.tarball))
        self.local_metadata_path = self.local_path + METADATA_FILE_EXT
        if not os.path.exists(self.local_path):
            try:
                self.s3.download_file(STAGING_BUCKET, self.tarball, self.local_path)
            except:
                logging.error(
                    f'Failed to download tarball {self.tarball} from {STAGING_BUCKET} to {local_tarball_path}.'
                )
                self.local_path = None
        if not os.path.exists(self.local_metadata_path):
            try:
                self.s3.download_file(STAGING_BUCKET, self.metadata_file, self.local_metadata_path)
            except:
                logging.error(
                    f'Failed to download metadata file {self.metadata_file} from {STAGING_BUCKET} to {local_metadata_path}.'
                )
                self.local_metadata_path = None


    def find_state(self):
        """Find the state of this tarball by searching through the state directories."""
        for state in list(self.states.keys()):
            # iterate through the state dirs and try to find the tarball's metadata file
            try:
                self.git_repo.get_contents(state + '/' + self.metadata_file)
                return state
            except github.GithubException:
                continue
        else:
            # if no state was found, we assume this is a new tarball that was ingested to the bucket
            return list(self.states.keys())[0]


    def next_state(self, state):
        """Find the next state for this tarball."""
        if state in self.states and 'next_state' in self.states[state]:
            return self.states[state]['next_state']
        else:
            return None


    def run_handler(self):
        """Process this tarball by running the process function that corresponds to the current state."""
        if not self.state:
            self.state = self.find_state()
        handler = self.states[self.state]['handler']
        handler()


    def ingest(self):
        """Process a tarball that is ready to be ingested by running the ingestion script."""
        tarball_path = self.download()
        #TODO: add verify function that verifies the checksum before ingesting
        ingest_cmd = subprocess.run(['echo', TARBALL_INGESTION_SCRIPT, tarball_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if ingest_cmd.returncode == 0:
            next_state = self.next_state(self.state)
            self.move_metadata_file(self.state, next_state)
        else:
            issue_title=f'Failed to ingest {self.tarball}'
            issue_body = FAILED_INGESTION_ISSUE_BODY.format(
                command=' '.join(ingest_cmd.args),
                tarball=self.tarball,
                return_code=ingest_cmd.returncode,
                stdout=ingest_cmd.stdout.decode('UTF-8'),
                stderr=ingest_cmd.stderr.decode('UTF-8'),
            )
            if self.issue_exists(issue_title, state='open'):
                print(f'Failed to ingest {self.tarball}, but an open issue already exists, skipping...')
            else:
                self.git_repo.create_issue(title=issue_title, body=issue_body)


    def print_ingested(self):
        """Process a tarball that has already been ingested."""
        print(f'{self.tarball} has already been ingested, skipping...')


    def mark_new_tarball_as_staged(self):
        """Process a new tarball that was added to the staging bucket."""
        next_state = self.next_state(self.state)
        # Download the tarball and its metadata file.
        self.download()
        if not self.local_path or not self.local_metadata_path:
            logging.warn('Skipping this tarball...')
            return

        contents = ''
        with open(self.local_metadata_path, 'r') as meta:
            contents = meta.read()

        file_path_staged = next_state + '/' + self.metadata_file
        print(file_path_staged)
        new_file = self.git_repo.create_file(file_path_staged, 'new tarball', contents, branch='main')

        self.state = next_state
        self.run_handler()


    def print_rejected(self):
        """Process a (rejected) tarball for which the corresponding PR has been closed witout merging."""
        print("This tarball was rejected, so we're skipping it.")
        # Do we want to delete rejected tarballs at some point?


    def make_approval_request(self):
        """Process a staged tarball by opening a pull request for ingestion approval."""
        next_state = self.next_state(self.state)
        file_path_staged = self.state + '/' + self.metadata_file
        file_path_to_ingest = next_state + '/' + self.metadata_file

        filename = os.path.basename(self.tarball)
        tarball_metadata = self.git_repo.get_contents(file_path_staged)
        git_branch = filename + '_' + next_state
        self.download()
        tar = tarfile.open(self.local_path, 'r')
        tar_dirs = sorted([m.path for m in tar.getmembers() if m.isdir()])
        pr_body = PR_BODY.format(tar_overview='\n'.join(tar_dirs))

        main_branch = self.git_repo.get_branch('main')
        if git_branch in [branch.name for branch in self.git_repo.get_branches()]:
            # Existing branch found for this tarball, so we've run this step before.
            # Try to find out if there's already a PR as well...
            print("Branch already exists for " + self.tarball)
            head = self.github.get_user().login + ':' + git_branch
            print(head)
            find_pr = list(self.git_repo.get_pulls(head=head, state='all'))
            print(find_pr)
            if find_pr:
                # So, we have a branch and a PR for this tarball (if there are more, pick the first one)...
                pr = find_pr.pop(0)
                print(f'PR {pr.number} found for {self.tarball}')
                if pr.state == 'open':
                    # The PR is still open, so it hasn't been reviewed yet: ignore this tarball.
                    print('PR is still open, skipping this tarball...')
                    return
                elif pr.state == 'closed' and not pr.merged:
                    # The PR was closed but not merged, i.e. it was rejected for ingestion.
                    print('PR was rejected')
                    self.reject()
                    return
                else:
                    print('Warning, tarball {tarball} is in a weird state:')
                    print(f'Branch: {git_branch}\nPR: {pr}\nPR state: {pr.state}\nPR merged: {pr.merged}')
            else:
                # There is a branch, but no PR for this tarball.
                # This is weird, so let's remove the branch and reprocess the tarball.
                print(f'Tarball {self.tarball} has a branch, but no PR.')
                print(f'Removing existing branch...')
                ref = self.git_repo.get_git_ref(f'heads/{git_branch}')
                ref.delete()

        # Create a new branch
        self.git_repo.create_git_ref(ref='refs/heads/' + git_branch, sha=main_branch.commit.sha)
        # Move the file to the directory of the next stage in this branch
        self.move_metadata_file(self.state, next_state, branch=git_branch)
        # Open a PR to get approval for the ingestion
        self.git_repo.create_pull(title='Ingest ' + filename, body=pr_body, head=git_branch, base='main')


    def move_metadata_file(self, old_state, new_state, branch='main'):
        """Move the metadata file of a tarball from an old state's directory to a new state's directory."""
        file_path_old = old_state + '/' + self.metadata_file
        file_path_new = new_state + '/' + self.metadata_file
        tarball_metadata = self.git_repo.get_contents(file_path_old)
        # Remove the metadata file from the old state's directory...
        self.git_repo.delete_file(file_path_old, 'remove from ' + old_state, sha=tarball_metadata.sha, branch=branch)
        # and move it to the new state's directory
        self.git_repo.create_file(file_path_new, 'move to ' + new_state, tarball_metadata.decoded_content, branch=branch)


    def reject(self):
        """Reject a tarball for ingestion."""
        # Let's move the the tarball to the directory for rejected tarballs.
        next_state = 'rejected'
        self.move_metadata_file(self.state, next_state)


    def issue_exists(self, title, state='open'):
        """Check if an issue with the given title and state already exists."""
        issues = self.git_repo.get_issues(state=state)
        for issue in issues:
            if issue.title == title and issue.state == state:
                return True
        else:
            return False


def read_github_token(filename):
    with open(filename, 'r') as tokenfile:
            token = tokenfile.read().strip()
    return token


def find_tarballs():
    # add credential check
    s3 = boto3.client('s3')
    tarballs = [
        object['Key']
        for object in s3.list_objects_v2(Bucket=STAGING_BUCKET)['Contents']
        if not object['Key'].endswith(METADATA_FILE_EXT)
    ]
    return tarballs


def main():
    token = read_github_token(GITHUB_TOKEN_FILE)
    gh = github.Github(token)
    git_repo = gh.get_repo(STAGING_REPO)

    #tarballs = find_tarballs()[-3:-2]
    #tarballs = find_tarballs()[-4:-3]
    tarballs = find_tarballs()

    for tarball in tarballs:
        print(tarball)
        tar = EessiTarball(tarball, gh)
        tar.run_handler()


if __name__ == '__main__':
    main()
