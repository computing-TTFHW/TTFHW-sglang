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
from collections import defaultdict


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


def parse_dockerfile_log(log_content, dockerfile_content=None):
    """
    Parse Buildkit log to extract Dockerfile stage timings.

    Log format:
    2026-04-10T09:06:45.2770303Z #1 [internal] booting buildkit
    2026-04-10T09:06:48.2219463Z #1 DONE 2.9s

    2026-04-10T09:06:50.3871078Z #5 [linux/amd64  1/12] FROM quay.io/...
    2026-04-10T09:08:37.1662964Z #5 DONE 106.7s
    """
    import re as regex
    stages = []

    lines = log_content.split('\n')
    print(f"    Parsing {len(lines)} lines of log...")

    # Pattern: timestampZ #N [...] command
    # e.g., 2026-04-10T09:06:45.2770303Z #1 [linux/amd64  1/12] FROM ...
    stage_pattern = regex.compile(r'\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+#(\d+)\s+\[([^\]]+)\]\s*(.+)')

    # Pattern for DONE: timestampZ #N DONE X.Xs
    # e.g., 2026-04-10T09:08:37.1662964Z #5 DONE 106.7s
    done_pattern = regex.compile(r'\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+#(\d+)\s+DONE\s+(\d+\.\d+s)')

    # Track stages by stage number
    stage_info = {}

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

    # Second pass: find all stage start lines
    seen_keys = set()
    for line in lines:
        match = stage_pattern.search(line)
        if match:
            stage_num = match.group(1)
            bracket = match.group(2).strip()  # e.g., "linux/amd64  1/12" or "internal"
            command = match.group(3).strip()

            # Extract platform and step info from bracket
            # Format: "linux/amd64  1/12" or "linux/amd64 1/12" or "internal"
            step_match = regex.search(r'(\d+)/(\d+)', bracket)
            if step_match:
                step_num = step_match.group(1)
                total_steps = step_match.group(2)
                platform = bracket[:step_match.start()].strip()
            else:
                step_num = None
                total_steps = None
                platform = bracket

            # Only process amd64 or non-platform-specific stages
            if 'amd64' not in platform and 'arm64' not in platform and step_num is None:
                continue

            # For multi-platform builds, we want amd64 only
            if 'arm64' in platform:
                continue

            # Skip if we already have this stage
            key = f"{stage_num}_{platform}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # Get duration from done_times
            duration = done_times.get(stage_num)

            # Extract instruction type from command
            instr_match = regex.match(r'^(\w+)', command)
            instruction = instr_match.group(1).upper() if instr_match else 'OTHER'

            if duration is not None:
                stage_info[key] = {
                    'stage_id': f"#{stage_num}",
                    'platform': platform,
                    'step': f"[{step_num}/{total_steps}]" if step_num else '',
                    'instruction_type': instruction,
                    'instruction_detail': command[:100],
                    'stage_info': bracket,
                    'command': command,
                    'duration': duration,
                    'duration_formatted': format_duration(duration),
                    'source_line': line[:150]
                }
                print(f"    [FOUND] #{stage_num} [{bracket}] {instruction} -> {duration:.1f}s")

    # Build final list sorted by step number then stage number
    def sort_key(k):
        info = stage_info[k]
        step = info.get('step', '[0/0]')
        step_match = regex.search(r'\[?(\d+)/(\d+)\]?', step)
        if step_match:
            return (int(step_match.group(1)), int(k.split('_')[0]))
        return (999, int(k.split('_')[0]))

    for key in sorted(stage_info.keys(), key=sort_key):
        stages.append(stage_info[key])

    print(f"    Final: {len(stages)} stages with timing")
    return stages


def download_and_parse_dockerfile(repo, gh_token):
    """
    Download npu.Dockerfile from sglang community repo and parse instructions.
    """
    import requests as req
    dockerfile_url = "https://raw.githubusercontent.com/sgl-project/sglang/main/docker/npu.Dockerfile"

    try:
        headers = {'Authorization': f'token {gh_token}'}
        resp = req.get(dockerfile_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            content = resp.text
            print(f"  Downloaded Dockerfile ({len(content)} bytes)")
            instructions = parse_dockerfile_for_instructions_from_content(content)
            return content, instructions
        else:
            print(f"  Failed to download Dockerfile: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  Error downloading Dockerfile: {e}")

    return None, []


def parse_dockerfile_for_instructions_from_content(content):
    """
    Parse Dockerfile content to extract instructions in order.
    Returns list of (instruction_type, command) tuples.
    """
    import re as regex
    instructions = []

    # Match lines starting with instruction keywords
    instruction_pattern = regex.compile(r'^(ARG|FROM|RUN|COPY|ADD|ENV|LABEL|WORKDIR|EXPOSE|CMD|ENTRYPOINT|USER|VOLUME|SHELL)\s+(.+)', regex.IGNORECASE | regex.MULTILINE)

    for match in instruction_pattern.finditer(content):
        instr_type = match.group(1).upper()
        command = match.group(2)[:100]
        instructions.append((instr_type, command))

    print(f"    Found {len(instructions)} instructions in Dockerfile")
    return instructions


def parse_dockerfile_for_instructions(dockerfile_path):
    """
    Parse Dockerfile to extract instructions in order.
    Returns list of (instruction_type, command) tuples.
    """
    import re as regex
    instructions = []

    try:
        with open(dockerfile_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"    Dockerfile not found: {dockerfile_path}")
        return instructions

    # Match lines starting with instruction keywords
    instruction_pattern = regex.compile(r'^(ARG|FROM|RUN|COPY|ADD|ENV|LABEL|WORKDIR|EXPOSE|CMD|ENTRYPOINT|USER|VOLUME|SHELL)\s+(.+)', regex.IGNORECASE | regex.MULTILINE)

    for match in instruction_pattern.finditer(content):
        instr_type = match.group(1).upper()
        command = match.group(2)[:100]
        instructions.append((instr_type, command))

    print(f"    Found {len(instructions)} instructions in Dockerfile")
    return instructions


def analyze_dockerfile_stages(stages):
    """
    Analyze Dockerfile stages and group by instruction type.

    Returns statistics about each instruction type.
    """
    by_type = defaultdict(list)
    for stage in stages:
        instr_type = stage.get('instruction_type', 'OTHER')
        by_type[instr_type].append(stage)

    analysis = {}
    for instr_type, type_stages in by_type.items():
        total_duration = sum(s.get('duration', 0) for s in type_stages)
        analysis[instr_type] = {
            'count': len(type_stages),
            'total_duration': total_duration,
            'total_duration_formatted': format_duration(total_duration),
            'avg_duration': total_duration / len(type_stages) if type_stages else 0,
            'stages': type_stages
        }

    return analysis


def generate_build_report(gh_token, run_id, repo, output_dir='.'):
    """Generate build time report from GitHub Actions workflow run."""

    headers = {
        'Authorization': f'token {gh_token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    # Get workflow run info
    run_url = f'https://api.github.com/repos/{repo}/actions/runs/{run_id}'
    run_resp = requests.get(run_url, headers=headers)
    run_resp.raise_for_status()
    run_data = run_resp.json()

    # Download and parse Dockerfile from sglang community repo
    print("Downloading npu.Dockerfile from sglang repo...")
    dockerfile_content, dockerfile_instructions = download_and_parse_dockerfile(repo, gh_token)
    print(f"  Dockerfile instructions: {len(dockerfile_instructions)}")
    for instr_type, cmd in dockerfile_instructions[:10]:
        print(f"    - {instr_type}: {cmd[:50]}")
    if len(dockerfile_instructions) > 10:
        print(f"    ... and {len(dockerfile_instructions) - 10} more")

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

                        # Find log files for this job
                        # GitHub log file naming pattern: {step_num}_{job_name}.txt
                        # e.g., "0_build-npu-image (8.5.0, 910b).txt"
                        job_id_str = str(job_id)
                        target_files = []
                        found_log_files = []  # Store found log files for this job

                        # Strategy 1: Match by job name (with or without step number prefix)
                        # Pattern: {num}_{job_name}.txt or {job_name}.txt
                        for name in log_zip.namelist():
                            if name.endswith('.txt'):
                                # Remove step number prefix like "0_" or "1_"
                                name_without_prefix = name
                                if name[0].isdigit() and '_' in name:
                                    name_without_prefix = name.split('_', 1)[1]

                                # Check if job name matches
                                if job_name in name or job_name in name_without_prefix:
                                    target_files.append(name)
                                    found_log_files.append(name)

                        # Strategy 2: Match by job ID in path
                        if not target_files:
                            for name in log_zip.namelist():
                                if f'_{job_id_str}/' in name or name.startswith(f'{job_name}_'):
                                    target_files.append(name)
                                    found_log_files.append(name)

                        # Strategy 3: Look for files containing "build" and job matrix values
                        if not target_files:
                            # Extract matrix values from job name (e.g., "8.5.0" and "910b" or "a3")
                            import re
                            matrix_matches = re.findall(r'\(([^)]+)\)', job_name)
                            if matrix_matches:
                                matrix_values = matrix_matches[0].split(', ')
                                for name in log_zip.namelist():
                                    if name.endswith('.txt'):
                                        # Check if all matrix values are in the filename
                                        if all(val in name for val in matrix_values):
                                            target_files.append(name)
                                            found_log_files.append(name)

                        # Strategy 4: Look for any file containing "build"
                        if not target_files:
                            for name in log_zip.namelist():
                                lower_name = name.lower()
                                if 'build' in lower_name and name.endswith('.txt'):
                                    target_files.append(name)
                                    found_log_files.append(name)

                        # Last resort: try all .txt files
                        if not target_files:
                            target_files = [n for n in log_zip.namelist() if n.endswith('.txt')]
                            found_log_files = target_files[:5]  # Limit to first 5

                        print(f"    Found {len(target_files)} potential log files for this job")

                        # Store found log files in job_info for HTML display
                        job_info['found_log_files'] = found_log_files

                        # Parse each log file and look for build output
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
                                        # Pass dockerfile_content to parser for better matching
                                        stages = parse_dockerfile_log(log_content, dockerfile_content)
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

                        if dockerfile_stages:
                            job_info['dockerfile_stages'] = dockerfile_stages
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
    all_stages = [s for j in build_report['jobs'] for s in j.get('dockerfile_stages', [])]

    # Analyze Dockerfile stages by instruction type
    dockerfile_analysis = analyze_dockerfile_stages(all_stages)

    build_report['summary'] = {
        'total_jobs': len(build_report['jobs']),
        'successful_jobs': sum(1 for j in build_report['jobs'] if j.get('conclusion') == 'success'),
        'failed_jobs': sum(1 for j in build_report['jobs'] if j.get('conclusion') == 'failure'),
        'total_dockerfile_stages': len(all_stages),
        'slowest_stages': sorted(
            all_stages,
            key=lambda x: x.get('duration', 0),
            reverse=True
        )[:10],
        'dockerfile_analysis': dockerfile_analysis
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
    html = html.replace('{{TOTAL_DOCKERFILE_STAGES}}', str(report['summary']['total_dockerfile_stages']))
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
        '''

        # Show log files found in the zip
        log_files = job.get('found_log_files', [])
        if log_files:
            job_html += f'''
            <div class="stages-section">
                <h3>Log Files Found in Workflow Zip ({len(log_files)} files)</h3>
                <p style="font-size: 12px; color: #8b949e; margin-bottom: 10px;">
                    These log files are saved to <code style="background: #161b22; padding: 2px 6px; border-radius: 4px;">logs/full-*.txt</code> for debugging
                </p>
                <ul style="font-family: monospace; font-size: 11px; color: #8b949e;">
            '''
            for lf in log_files[:30]:  # Show max 30 files
                # Escape special HTML characters
                lf_escaped = lf.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                job_html += f'<li>{lf_escaped}</li>'
            if len(log_files) > 30:
                job_html += f'<li>... and {len(log_files) - 30} more</li>'
            job_html += '''
                </ul>
            </div>
            '''

        # Dockerfile Stages
        dockerfile_stages = job.get('dockerfile_stages', [])
        if dockerfile_stages:
            job_html += f'''
            <div class="stages-section">
                <h3>Dockerfile Build Stages ({len(dockerfile_stages)} stages)</h3>
            '''

            for stage in dockerfile_stages:
                stage_id = stage.get('stage_id', '#N/A')
                stage_info = stage.get('stage_info', '')
                stage_cmd = stage.get('command', '')
                stage_duration = stage.get('duration_formatted', 'N/A')
                instr_type = stage.get('instruction_type', 'OTHER')
                instr_detail = stage.get('instruction_detail', '')

                job_html += f'''
                <div class="stage-card">
                    <div class="stage-header">
                        <div>
                            <span class="stage-id">{stage_id}</span>
                            <span class="instruction-type instr-{instr_type}">{instr_type}</span>
                            <span class="stage-info">{stage_info}</span>
                        </div>
                        <span class="stage-duration">{stage_duration}</span>
                    </div>
                    <div class="stage-command">{instr_detail}</div>
                </div>
                '''

            job_html += '''
            </div>
            '''

        job_html += '''
        </div>
        '''
        jobs_html += job_html

    html = html.replace('{{JOBS_CONTENT}}', jobs_html)

    # Generate Dockerfile instruction summary HTML
    dockerfile_analysis = report['summary'].get('dockerfile_analysis', {})
    instruction_summary_html = '<div class="instruction-summary">'

    if dockerfile_analysis:
        # Sort by total duration (descending)
        sorted_types = sorted(
            dockerfile_analysis.items(),
            key=lambda x: x[1]['total_duration'],
            reverse=True
        )

        for instr_type, data in sorted_types:
            instruction_summary_html += f'''
            <div class="instr-card">
                <div class="type"><span class="instruction-type instr-{instr_type}">{instr_type}</span></div>
                <div class="count">{data['count']} stages</div>
                <div class="duration">Total: {data['total_duration_formatted']}</div>
            </div>
            '''

    instruction_summary_html += '</div>'

    # Handle empty case
    if not dockerfile_analysis:
        instruction_summary_html = '<p style="color: #8b949e;">No Dockerfile instruction data available</p>'

    html = html.replace('{{DOCKERFILE_INSTRUCTION_SUMMARY}}', instruction_summary_html)

    # Generate slowest stages HTML
    slowest = report['summary'].get('slowest_stages', [])
    slowest_html = ""
    if slowest:
        for idx, stage in enumerate(slowest, 1):
            stage_info = stage.get('stage_info', '')
            stage_cmd = stage.get('command', '')[:80]
            stage_duration = stage.get('duration_formatted', 'N/A')
            instr_type = stage.get('instruction_type', 'OTHER')

            slowest_html += f'''
            <div class="slowest-item">
                <div class="slowest-rank">{idx}</div>
                <div class="slowest-details">
                    <span class="instruction-type instr-{instr_type}">{instr_type}</span>
                    <div class="slowest-stage">{stage_info} - {stage_cmd}...</div>
                </div>
                <div class="slowest-duration">{stage_duration}</div>
            </div>
            '''
        html = html.replace('{{SLOWEST_STAGES_CONTENT}}', slowest_html)
    else:
        html = html.replace('{{SLOWEST_STAGES_CONTENT}}', '<p style="color: #8b949e;">No Dockerfile stages data available</p>')

    html_path = os.path.join(output_dir, 'build-report.html')
    with open(html_path, 'w') as f:
        f.write(html)

    print(f"HTML report generated: {html_path}")
    return html_path


def main():
    """Main entry point."""
    gh_token = os.environ.get('GH_TOKEN')
    run_id = os.environ.get('RUN_ID')
    repo = os.environ.get('REPO')
    output_dir = os.environ.get('OUTPUT_DIR', '.')

    if not all([gh_token, run_id, repo]):
        print("Error: Missing required environment variables")
        print("Required: GH_TOKEN, RUN_ID, REPO")
        print("Optional: OUTPUT_DIR (default: '.')")
        exit(1)

    try:
        print(f"Generating build report for run {run_id} in {repo}...")

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
