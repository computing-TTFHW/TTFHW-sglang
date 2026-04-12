#!/usr/bin/env python3
"""
Build Time Report Generator

Parses GitHub Actions workflow logs to extract timing information
for workflow steps and Dockerfile build stages.
Generates JSON and HTML reports.
"""

import os
import json
import re
import zipfile
import io
import requests
from datetime import datetime


# ANSI escape code pattern
ANSI_PATTERN = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def strip_ansi(text):
    """Remove ANSI escape codes from text."""
    return ANSI_PATTERN.sub('', text)


def parse_time(time_str):
    """Parse ISO format time string to datetime object."""
    if time_str and time_str != 'unknown':
        try:
            return datetime.fromisoformat(time_str.replace('Z', '+00:00'))
        except:
            return None
    return None


def format_duration(seconds):
    """Format duration in human-readable format."""
    if seconds is None:
        return "N/A"
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.1f}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def parse_dockerfile_log(log_content):
    """
    Parse Buildkit log to extract Dockerfile stage timings.

    Match ALL lines with format: timestampZ #N ...
    Examples:
    2026-04-10T09:06:45.2770303Z #1 [internal] booting buildkit
    2026-04-10T09:06:48.2219463Z #1 DONE 2.9s

    2026-04-10T09:06:50.3871078Z #5 [linux/amd64  1/12] FROM quay.io/...
    2026-04-10T09:08:37.1662964Z #5 DONE 106.7s

    2026-04-10T09:54:11.4655416Z #34 resolving provenance for metadata file
    2026-04-10T09:54:11.4736616Z #34 DONE 0.0s
    """
    import re as regex
    stages = []

    lines = log_content.split('\n')
    print(f"    Parsing {len(lines)} lines of log...")

    # Pattern 1: timestampZ #N DONE X.Xs  (match from start of line)
    done_pattern = regex.compile(r'^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+#(\d+)\s+DONE\s+(\d+\.\d+s)', regex.MULTILINE)

    # Pattern 2: timestampZ #N [...] (with bracket) - match from start of line
    bracket_pattern = regex.compile(r'^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+#(\d+)\s+\[([^\]]+)\]\s*(.+)', regex.MULTILINE)

    # Pattern 3: timestampZ #N (without bracket) - match from start of line
    simple_pattern = regex.compile(r'^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+#(\d+)\s+(\S.*)$', regex.MULTILINE)

    # First pass: find all DONE lines and their times
    done_times = {}
    for line in lines:
        match = done_pattern.search(line)
        if match:
            stage_num = match.group(1)
            duration_str = match.group(2)
            duration_sec = float(duration_str.replace('s', ''))
            done_times[stage_num] = duration_sec

    print(f"    Found {len(done_times)} DONE lines")
    for sn, t in sorted(done_times.items(), key=lambda x: int(x[0])):
        print(f"      #{sn}: {t:.1f}s")

    # Second pass: find the FIRST start line for each stage number
    # Each stage number should appear only once as a start line
    seen_stage_nums = set()
    for idx, line in enumerate(lines):
        stage_num = None
        bracket = None
        command = None

        # Try bracket pattern first (this matches lines with [...])
        match = bracket_pattern.search(line)
        if match:
            stage_num = match.group(1)
            bracket = match.group(2).strip()
            command = match.group(3).strip()
        else:
            # Try simple pattern (no bracket) - matches lines like "#34 resolving provenance..."
            match = simple_pattern.search(line)
            if match:
                stage_num = match.group(1)
                bracket = ''
                command = match.group(2).strip()
                # Skip DONE lines in simple pattern
                if 'DONE' in command:
                    continue

        if stage_num is None:
            continue

        # Skip if we already processed this stage number
        if stage_num in seen_stage_nums:
            print(f"    [SKIP DUP] #{stage_num} at line {idx}: {line[:100]}")
            continue
        seen_stage_nums.add(stage_num)

        # Print the full line for debugging
        print(f"    [PARSE] Line {idx}: {line[:120]}")

        # Get duration from done_times
        duration = done_times.get(stage_num)

        # Extract platform and step info from bracket (e.g., "linux/amd64 5/12")
        platform = ''
        step_info = ''
        step_match = regex.search(r'(\d+)/(\d+)', bracket) if bracket else None
        if step_match:
            step_info = f"[{step_match.group(1)}/{step_match.group(2)}]"
            # Platform is before the step info
            platform_part = bracket[:step_match.start()].strip()
            platform = platform_part.split()[0] if platform_part else ''

        # Extract instruction type
        if bracket and bracket in ['auth', 'internal', 'exporting', 'sending', 'writing']:
            instruction = bracket.upper()
        elif bracket:
            # Extract from command or bracket
            if step_match:
                cmd_part = bracket[step_match.end():].strip()
                instr_match = regex.match(r'^(\w+)', cmd_part)
                instruction = instr_match.group(1).upper() if instr_match else 'OTHER'
            else:
                instr_match = regex.match(r'^(\w+)', command)
                instruction = instr_match.group(1).upper() if instr_match else 'OTHER'
        else:
            instr_match = regex.match(r'^(\w+)', command)
            instruction = instr_match.group(1).upper() if instr_match else 'OTHER'

        if duration is not None:
            stage_data = {
                'stage_id': f"#{stage_num}",
                'platform': platform,
                'step': step_info,
                'instruction_type': instruction,
                'instruction_detail': command[:100] if command else bracket,
                'stage_info': bracket,
                'command': command if command else bracket,
                'duration': duration,
                'duration_formatted': format_duration(duration),
                'source_line': line[:150]
            }
            bracket_str = f"[{bracket}]" if bracket else ""
            print(f"    [FOUND] #{stage_num} {bracket_str} {command[:80] if command else ''} -> {duration:.1f}s")
            stages.append(stage_data)
        else:
            # No DONE time found for this stage
            bracket_str = f"[{bracket}]" if bracket else ""
            print(f"    [SKIP] #{stage_num} {bracket_str} - No DONE time found")

    # Build final list sorted by stage number
    stages.sort(key=lambda x: int(x['stage_id'].replace('#', '')))

    print(f"    Final: {len(stages)} stages with timing")
    return stages


def generate_build_report(gh_token, run_id, repo='', output_dir='.'):
    """Generate build time report from GitHub Actions workflow run.

    Args:
        gh_token: GitHub token for API access
        run_id: Workflow run ID to analyze
        repo: Repository name (e.g., 'owner/repo'). If empty, auto-detect from run_id
        output_dir: Output directory for reports
    """

    headers = {
        'Authorization': f'token {gh_token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    # Get workflow run info
    # If repo is not provided, auto-detect from the run_id's workflow run
    if repo:
        run_url = f'https://api.github.com/repos/{repo}/actions/runs/{run_id}'
    else:
        # First, get the run to find out which repo it belongs to
        run_url = f'https://api.github.com/repos/OWNER_PLACEHOLDER/actions/runs/{run_id}'

    run_resp = requests.get(run_url, headers=headers)
    run_resp.raise_for_status()
    run_data = run_resp.json()

    # Auto-detect repo from run_data if not provided
    if not repo:
        repo = run_data.get('repository', {}).get('full_name', '')
        if not repo:
            raise ValueError(f"Could not auto-detect repository from run_id {run_id}")
        print(f"Auto-detected repository: {repo}")

    # Update run_url with the correct repo
    run_url = f'https://api.github.com/repos/{repo}/actions/runs/{run_id}'

    # Get all jobs for this run (with pagination support)
    all_jobs = []
    jobs_url = f'https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs'

    print(f"Fetching jobs for run {run_id}...")
    print(f"  API URL: {jobs_url}")

    try:
        while jobs_url:
            jobs_resp = requests.get(jobs_url, headers=headers)
            jobs_resp.raise_for_status()
            jobs_data = jobs_resp.json()
            new_jobs = jobs_data.get('jobs', [])
            print(f"  Found {len(new_jobs)} jobs in this page")
            all_jobs.extend(new_jobs)
            # Handle pagination
            jobs_url = jobs_resp.links.get('next', {}).get('url')
    except Exception as e:
        print(f"Error fetching jobs: {e}")
        import traceback
        traceback.print_exc()
        raise

    print(f"Total jobs to process: {len(all_jobs)}")

    # Print job names for debugging
    for job in all_jobs:
        print(f"  - Job: {job.get('name')} (ID: {job.get('id')}, Status: {job.get('status')})")

    build_report = {
        'workflow_name': run_data.get('name', 'Unknown'),
        'run_id': run_id,
        'workflow_run_url': run_data.get('html_url', ''),
        'trigger': run_data.get('event', 'workflow_dispatch'),
        'branch': run_data.get('head_branch', 'unknown'),
        'commit': run_data.get('head_sha', 'unknown'),
        'created_at': run_data.get('created_at', 'unknown'),
        'updated_at': run_data.get('updated_at', 'unknown'),
        'jobs': []
    }

    for job in all_jobs:
        # Skip jobs that are not completed
        if job.get('status') != 'completed':
            continue

        print(f"Processing job: {job.get('name', 'Unknown')} (ID: {job.get('id')})...")

        job_started = parse_time(job.get('started_at'))
        job_completed = parse_time(job.get('completed_at'))

        job_info = {
            'job_name': job.get('name', 'Unknown'),
            'job_id': job.get('id'),
            'status': job.get('status', 'unknown'),
            'conclusion': job.get('conclusion', 'unknown'),
            'started_at': job.get('started_at', 'unknown'),
            'completed_at': job.get('completed_at', 'unknown'),
            'steps': [],
            'dockerfile_stages': []
        }

        if job_started and job_completed:
            duration = (job_completed - job_started).total_seconds()
            job_info['duration_seconds'] = duration
            job_info['duration_formatted'] = format_duration(duration)

        # Get detailed job steps
        if job.get('id'):
            job_steps_url = f"https://api.github.com/repos/{repo}/actions/jobs/{job['id']}"
            steps_resp = requests.get(job_steps_url, headers=headers)
            if steps_resp.status_code == 200:
                job_detail = steps_resp.json()
                for idx, step in enumerate(job_detail.get('steps', [])):
                    step_info = {
                        'step_number': idx + 1,
                        'name': step.get('name', 'Unknown'),
                        'status': step.get('status', 'unknown'),
                        'conclusion': step.get('conclusion', 'unknown'),
                        'started_at': step.get('started_at', 'unknown'),
                        'completed_at': step.get('completed_at', 'unknown')
                    }

                    step_started = parse_time(step.get('started_at'))
                    step_completed = parse_time(step.get('completed_at'))
                    if step_started and step_completed:
                        step_duration = (step_completed - step_started).total_seconds()
                        step_info['duration_seconds'] = step_duration
                        step_info['duration_formatted'] = format_duration(step_duration)

                    job_info['steps'].append(step_info)

        # Get job logs for Dockerfile parsing
        # GitHub returns workflow logs as a zip file with separate log files per job/step
        if job.get('status') == 'completed':
            job_name = job.get('name', 'Unknown')
            job_id = job.get('id')
            print(f"  Looking for Docker build output in job '{job_name}' (ID: {job_id})...")

            if run_id:
                # Download workflow run logs (returns a zip file)
                workflow_log_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/logs"
                workflow_log_resp = requests.get(workflow_log_url, headers=headers)

                if workflow_log_resp.status_code == 200:
                    try:
                        # GitHub returns a zip file containing log files for each step of each job
                        log_zip = zipfile.ZipFile(io.BytesIO(workflow_log_resp.content))

                        # List all files in zip for debugging
                        print(f"    ========== ZIP FILE CONTENTS ==========")
                        print(f"    Zip contains {len(log_zip.namelist())} files:")
                        all_log_files = log_zip.namelist()
                        for idx, name in enumerate(all_log_files):
                            print(f"      [{idx}] {name}")
                        print(f"    ============================")

                        # Store all log file names for this job in job_info
                        job_info['log_files'] = all_log_files

                        # Find log files for THIS job only
                        # GitHub log file naming: {step_num}_{job_name}.txt
                        # e.g., "0_build (8.5.0, 910b).txt" for job "build-npu-image (8.5.0, 910b)"
                        # Extract matrix values from job name (e.g., "8.5.0" and "910b" from "build-npu-image (8.5.0, 910b)")
                        import re
                        matrix_matches = re.findall(r'\(([^)]+)\)', job_name)
                        matrix_values = matrix_matches[0].split(', ') if matrix_matches else []

                        target_files = []
                        for name in log_zip.namelist():
                            if name.endswith('.txt') and 'system' not in name.lower():
                                # Check if all matrix values are in the filename
                                if matrix_values and all(val in name for val in matrix_values):
                                    target_files.append(name)
                                    break  # Found the log file for this job

                        # Fallback: try matching by job name
                        if not target_files:
                            for name in log_zip.namelist():
                                if name.endswith('.txt') and 'system' not in name.lower():
                                    if job_name in name:
                                        target_files.append(name)
                                        break

                        print(f"    Found {len(target_files)} log file(s) for job '{job_name}' (matrix: {matrix_values})")

                        # Parse log file(s) for this job only
                        # Each job has its own stages - no dedup needed across files
                        dockerfile_stages = []
                        for log_filename in target_files:
                            print(f"    ========== Processing log file: {log_filename} ==========")
                            try:
                                with log_zip.open(log_filename) as log_file:
                                    raw_log = log_file.read().decode('utf-8', errors='ignore')
                                    log_content = strip_ansi(raw_log)

                                    # Save full log for debugging using original filename
                                    log_dir = f'logs'
                                    os.makedirs(log_dir, exist_ok=True)
                                    full_log_path = f'{log_dir}/{log_filename.replace("/", "_")}'
                                    with open(full_log_path, 'w', encoding='utf-8') as f:
                                        f.write(log_content)
                                    print(f"      Saved full log ({len(log_content)} bytes) to {full_log_path}")

                                    # Count lines containing # and DONE for quick verification
                                    hash_lines = sum(1 for l in log_content.split('\n') if '#' in l and '[' in l)
                                    done_lines = sum(1 for l in log_content.split('\n') if 'DONE' in l)
                                    print(f"      Log stats: {hash_lines} stage lines, {done_lines} DONE lines")

                                    # Check for BuildKit output
                                    has_buildkit = '#[' in log_content or '# DONE' in log_content or 'DONE' in log_content

                                    if has_buildkit:
                                        print(f"      BuildKit output detected")
                                        stages = parse_dockerfile_log(log_content)
                                        if stages:
                                            print(f"      ✓ Parsed {len(stages)} Dockerfile stages")
                                            dockerfile_stages.extend(stages)
                                        else:
                                            print(f"      ✗ No stages parsed (check debug output above)")
                                    else:
                                        print(f"      No BuildKit output found")
                            except Exception as e:
                                print(f"      Error parsing {log_filename}: {e}")
                                import traceback
                                traceback.print_exc()

                        # Sort stages by stage number
                        dockerfile_stages.sort(key=lambda x: int(x['stage_id'].replace('#', '')))

                        if dockerfile_stages:
                            # Find the "Build and push Docker image" step
                            dockerbuild_step_idx = None

                            for idx, step in enumerate(job_info['steps']):
                                if step.get('name', '') == 'Build and push Docker image':
                                    dockerbuild_step_idx = idx
                                    break

                            if dockerbuild_step_idx is not None:
                                job_info['steps'][dockerbuild_step_idx]['dockerfile_stages'] = dockerfile_stages
                                print(f"  Attached {len(dockerfile_stages)} Dockerfile stages to step 'Build and push Docker image'")
                            else:
                                # Fallback: attach to job if step not found
                                job_info['dockerfile_stages'] = dockerfile_stages
                                print(f"  'Build and push Docker image' step not found, attached to job")

                            print(f"  Total: Found {len(dockerfile_stages)} Dockerfile stages for this job")

                            # Save parsed stages for debugging
                            safe_job_name = job_name.replace('/', '-').replace(' ', '_')
                            stages_file = f'logs/stages-{safe_job_name}.json'
                            with open(stages_file, 'w') as f:
                                json.dump(dockerfile_stages, f, indent=2, ensure_ascii=False)
                            print(f"    Stages saved to {stages_file}")
                        else:
                            print(f"    No Dockerfile stages found in any log file for this job")

                    except zipfile.BadZipfile:
                        print(f"    Response is not a valid zip file")
                        import traceback
                        traceback.print_exc()
                    except Exception as e:
                        print(f"    Error processing workflow logs: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    print(f"    Could not get workflow logs: HTTP {workflow_log_resp.status_code}")

        build_report['jobs'].append(job_info)

    # Calculate summary statistics
    build_report['summary'] = {
        'total_jobs': len(build_report['jobs']),
        'successful_jobs': sum(1 for j in build_report['jobs'] if j.get('conclusion') == 'success'),
        'failed_jobs': sum(1 for j in build_report['jobs'] if j.get('conclusion') == 'failure')
    }

    # Save JSON report
    json_path = os.path.join(output_dir, 'build-report.json')
    with open(json_path, 'w') as f:
        json.dump(build_report, f, indent=2, ensure_ascii=False)

    print(f"JSON report generated: {json_path}")
    print(f"Total jobs: {build_report['summary']['total_jobs']}")
    print(f"Successful: {build_report['summary']['successful_jobs']}")
    print(f"Failed: {build_report['summary']['failed_jobs']}")

    return build_report


def generate_html_report(report, output_dir='.'):
    """Generate HTML visualization report from build report data."""

    # Load HTML template
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        'scripts',
        'report_templates',
        'report_template.html'
    )

    with open(template_path, 'r') as f:
        template = f.read()

    # Replace placeholders with actual data
    html = template
    html = html.replace('{{WORKFLOW_NAME}}', report['workflow_name'])
    html = html.replace('{{WORKFLOW_RUN_URL}}', report.get('workflow_run_url', '#'))
    html = html.replace('{{RUN_ID}}', str(report['run_id']))
    html = html.replace('{{TRIGGER}}', report['trigger'])
    html = html.replace('{{BRANCH}}', report['branch'])
    html = html.replace('{{COMMIT}}', report['commit'][:8])
    html = html.replace('{{CREATED_AT}}', report['created_at'])
    html = html.replace('{{UPDATED_AT}}', report['updated_at'])
    html = html.replace('{{SUCCESSFUL_JOBS}}', str(report['summary']['successful_jobs']))
    html = html.replace('{{FAILED_JOBS}}', str(report['summary']['failed_jobs']))
    html = html.replace('{{TOTAL_JOBS}}', str(len(report['jobs'])))

    # Generate jobs HTML
    jobs_html = ""
    for job in report['jobs']:
        status_class = f"status-{job.get('conclusion', 'pending')}"
        status_text = job.get('conclusion', 'unknown')
        duration = job.get('duration_formatted', 'N/A')
        job_name = job.get('job_name', 'Unknown')

        job_html = f'''
        <div class="job-card">
            <div class="job-header">
                <span class="job-name">{job_name}</span>
                <div class="job-meta">
                    <span class="job-status {status_class}">{status_text}</span>
                    <span class="duration-badge">Duration: {duration}</span>
                </div>
            </div>

            <div class="steps-section">
                <h3>Workflow Steps</h3>
                <table class="steps-table">
                    <thead>
                        <tr>
                            <th style="width: 50%;">Step</th>
                            <th style="width: 15%;">Status</th>
                            <th style="width: 15%;">Duration</th>
                            <th style="width: 20%;">Start Time</th>
                        </tr>
                    </thead>
                    <tbody>
        '''

        for step in job.get('steps', []):
            step_status = step.get('conclusion', step.get('status', 'unknown'))
            step_duration = step.get('duration_formatted', 'N/A')
            step_started = step.get('started_at', 'N/A')
            if step_started and step_started != 'N/A':
                try:
                    dt = datetime.fromisoformat(step_started.replace('Z', '+00:00'))
                    step_started = dt.strftime('%H:%M:%S')
                except:
                    pass

            job_html += f'''
                        <tr>
                            <td class="step-name">{step.get('name', 'Unknown')}</td>
                            <td><span class="job-status status-{step_status}" style="font-size: 11px;">{step_status}</span></td>
                            <td class="step-duration">{step_duration}</td>
                            <td style="color: #8b949e; font-size: 13px;">{step_started}</td>
                        </tr>
            '''

        job_html += '''
                    </tbody>
                </table>
            </div>
        </div>
        '''
        jobs_html += job_html

    html = html.replace('{{JOBS_CONTENT}}', jobs_html)

    html_path = os.path.join(output_dir, 'build-report.html')
    with open(html_path, 'w') as f:
        f.write(html)

    print(f"HTML report generated: {html_path}")
    return html_path


def main():
    """Main entry point."""
    gh_token = os.environ.get('GH_TOKEN')
    run_id = os.environ.get('RUN_ID')
    # repo is optional: if not provided, use the repository from the run_id's workflow run
    repo = os.environ.get('REPO', '')
    output_dir = os.environ.get('OUTPUT_DIR', '.')

    if not all([gh_token, run_id]):
        print("Error: Missing required environment variables")
        print("Required: GH_TOKEN, RUN_ID")
        print("Optional: REPO (default: auto-detect from run_id), OUTPUT_DIR (default: '.')")
        exit(1)

    try:
        print(f"Generating build report for run {run_id}...")
        if repo:
            print(f"  Repository: {repo}")
        else:
            print(f"  Repository: auto-detect from run_id")

        # Generate JSON report
        report = generate_build_report(gh_token, run_id, repo, output_dir)

        # Generate HTML report
        generate_html_report(report, output_dir)

        print("Build report generation complete!")
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        exit(1)


if __name__ == '__main__':
    main()
