#!/usr/bin/env python3
"""
GitHub PR Review Tool

A reusable tool for posting code reviews to GitHub PRs with inline comments.
Can be used in GitHub Actions or run locally.

Usage:
    python gh_review_tool.py <owner> <repo> <pr_number> <review_data_json>

Environment Variables:
    GITHUB_TOKEN: GitHub token for authentication (optional, gh cli will use its own auth)

Example review_data.json:
{
  "body": "## Review Summary\n...",
  "event": "COMMENT",
  "comments": [
    {
      "file": "path/to/file.py",
      "search_line": "+        new_parameter: str,",
      "body": "**Suggestion**: Consider...",
      "fallback_line": 42
    }
  ]
}

Sourced from https://github.com/Constructor-io/autocomplete/blob/master/.github/scripts/gh_review_tool.py
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError


class GitHubReviewTool:
    def __init__(self, owner: str, repo: str, pr_number: int):
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.files_cache = None

    def get_pr_files(self) -> List[Dict]:
        """Fetch PR files from GitHub API."""
        if self.files_cache is not None:
            return self.files_cache

        result = subprocess.run(
            ['gh', 'api', f'/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/files'],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        self.files_cache = json.loads(result.stdout)
        return self.files_cache

    def find_file_by_path(self, file_path: str) -> Optional[Dict]:
        """Find a file in the PR by its path."""
        files = self.get_pr_files()
        for file_info in files:
            if file_info['filename'] == file_path or file_info['filename'].endswith('/' + file_path):
                return file_info
        return None

    def find_line_in_patch(
        self, patch: str, search_content: str, search_type: str = 'added', filename: str = 'file'
    ) -> Tuple[Optional[int], Optional[str]]:
        """
        Find the line number in the patch for a given content using unidiff library.

        Args:
            patch: The git diff patch (GitHub API format without file headers)
            search_content: The content to search for (can include +/- prefix or not)
            search_type: 'added', 'deleted', 'context', or 'any'
            filename: The filename for constructing a proper diff header

        Returns:
            (line_number, side) where side is 'RIGHT' for additions or 'LEFT' for deletions
        """
        # Normalize search content - remove +/- prefix if present
        search_content = search_content.strip()
        if search_content.startswith('+') or search_content.startswith('-'):
            search_content = search_content[1:].strip()

        try:
            # GitHub API returns patches without file headers, but unidiff needs them
            # Add proper diff headers for unidiff to parse
            full_patch = f"--- a/{filename}\n+++ b/{filename}\n{patch}"

            # Parse the patch using unidiff
            patch_set = PatchSet(full_patch)

            # Iterate through all files in the patch (should only be one in our case)
            for patched_file in patch_set:
                # Iterate through all chunks in the file
                for chunk in patched_file:
                    # First pass: try exact match
                    for line in chunk:
                        line_content = line.value.strip()
                        if search_content != line_content:
                            continue
                        if line.is_added and search_type in ['added', 'any']:
                            return line.target_line_no, 'RIGHT'
                        elif line.is_removed and search_type in ['deleted', 'any']:
                            return line.source_line_no, 'LEFT'
                        elif line.is_context and search_type in ['context', 'any']:
                            return line.target_line_no, 'RIGHT'

                    # Second pass: fall back to substring match
                    for line in chunk:
                        line_content = line.value.strip()
                        if search_content not in line_content:
                            continue
                        if line.is_added and search_type in ['added', 'any']:
                            return line.target_line_no, 'RIGHT'
                        elif line.is_removed and search_type in ['deleted', 'any']:
                            return line.source_line_no, 'LEFT'
                        elif line.is_context and search_type in ['context', 'any']:
                            return line.target_line_no, 'RIGHT'

        except (UnidiffParseError, IndexError) as e:
            # UnidiffParseError covers parsing issues, which can happen if the patch format is unexpected
            # IndexError covers malformed patch access
            print(f"Warning: Error parsing patch with unidiff: {e}")
            return None, None

        return None, None

    def prepare_comment(self, comment_spec: Dict) -> Optional[Dict]:
        """
        Prepare a comment for the GitHub API.

        Args:
            comment_spec: Dictionary with:
                - file: file path
                - search_line: content to search for in the diff
                - body: comment body
                - fallback_line: optional line number to use if search fails
                - search_type: optional 'added', 'deleted', 'context', or 'any' (default: 'added')

        Returns:
            GitHub API comment dict or None if line couldn't be found
        """
        file_path = comment_spec['file']
        search_content = comment_spec['search_line']
        comment_body = comment_spec['body']
        fallback_line = comment_spec.get('fallback_line')
        search_type = comment_spec.get('search_type', 'added')

        # Find the file in the PR
        file_info = self.find_file_by_path(file_path)
        if not file_info:
            print(f"Warning: File not found in PR: {file_path}")
            return None

        patch = file_info.get('patch')
        if not patch:
            print(f"Warning: No patch available for {file_path}, skipping")
            return None

        # Try to find the line in the patch
        line_num, side = self.find_line_in_patch(
            file_info['patch'], search_content, search_type, file_info['filename']
        )

        if line_num is None:
            if fallback_line:
                print(
                    f"Warning: Could not find line in {file_path},"
                    f" using fallback line {fallback_line}"
                )
                line_num = fallback_line
                side = 'RIGHT'
            else:
                print(f"Error: Could not find line in {file_path} for: {search_content[:50]}...")
                return None

        return {"path": file_info['filename'], "line": line_num, "side": side, "body": comment_body}

    def submit_review(self, review_body: str, event: str, comments: List[Dict]) -> Dict:
        """
        Submit a review to GitHub.

        Args:
            review_body: The main review body text
            event: 'COMMENT', 'APPROVE', or 'REQUEST_CHANGES'
            comments: List of prepared comments

        Returns:
            GitHub API response
        """
        review_data = {"body": review_body, "event": event, "comments": comments}

        # Write to temp file for input
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(review_data, f, indent=2)
            temp_file = f.name

        try:
            result = subprocess.run(
                [
                    'gh',
                    'api',
                    '--method',
                    'POST',
                    f'/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/reviews',
                    '--input',
                    temp_file,
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
            return json.loads(result.stdout)
        finally:
            os.unlink(temp_file)

    def post_review(self, review_spec: Dict) -> Dict:
        """
        Post a complete review given a specification.

        Args:
            review_spec: Dictionary with:
                - body: review body text
                - event: optional, one of 'COMMENT', 'APPROVE', 'REQUEST_CHANGES' (default: 'COMMENT')
                - comments: list of comment specifications

        Returns:
            GitHub API response
        """
        body = review_spec.get('body', '')
        allowed_events = {'COMMENT', 'APPROVE', 'REQUEST_CHANGES'}
        event = review_spec.get('event', 'COMMENT')
        if event not in allowed_events:
            print(f"Warning: Invalid event '{event}', defaulting to 'COMMENT'")
            event = 'COMMENT'
        comment_specs = review_spec.get('comments', [])

        # Prepare all comments
        prepared_comments = []
        for spec in comment_specs:
            comment = self.prepare_comment(spec)
            if comment:
                prepared_comments.append(comment)
            else:
                print("Warning: Skipping comment that could not be prepared")

        if not prepared_comments and comment_specs:
            print("Warning: No comments could be prepared, posting review without inline comments")

        # Submit the review
        return self.submit_review(body, event, prepared_comments)


def main():
    parser = argparse.ArgumentParser(
        description='Post a code review to a GitHub PR with inline comments'
    )
    parser.add_argument('owner', help='Repository owner')
    parser.add_argument('repo', help='Repository name')
    parser.add_argument('pr_number', type=int, help='Pull request number')
    parser.add_argument('review_data', help='Path to JSON file with review data or JSON string')
    parser.add_argument(
        '--dry-run', action='store_true', help='Show what would be posted without actually posting'
    )

    args = parser.parse_args()

    # Load review data
    try:
        # Try to parse as JSON string first
        review_spec = json.loads(args.review_data)
    except json.JSONDecodeError:
        # Try to load as file path
        try:
            with open(args.review_data, 'r') as f:
                review_spec = json.load(f)
        except Exception as e:
            print(f"Error: Could not parse review data as JSON or load as file: {e}")
            sys.exit(1)

    # Create tool and post review
    tool = GitHubReviewTool(args.owner, args.repo, args.pr_number)

    if args.dry_run:
        print("DRY RUN MODE - Would post the following review:")
        print(json.dumps(review_spec, indent=2))

        print("\nPreparing comments...")
        for spec in review_spec.get('comments', []):
            comment = tool.prepare_comment(spec)
            if comment:
                print(f"\n✓ Comment prepared for {comment['path']}:{comment['line']}")
                print(f"  {comment['body'][:80]}...")
            else:
                print(f"\n✗ Could not prepare comment for {spec['file']}")

        print("\nDry run complete. No review was posted.")
        return

    try:
        response = tool.post_review(review_spec)
        print("✓ Review posted successfully!")
        print(f"  Review ID: {response['id']}")
        print(f"  URL: {response['html_url']}")
        print(f"  State: {response['state']}")
    except subprocess.CalledProcessError as e:
        print(f"Error posting review: {e}")
        print(f"stderr: {e.stderr}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
