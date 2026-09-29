"""Sanity test for filters.py + dedup.py against the junior DevOps /
Linux / cloud search config (Baltimore, DC, Northern Virginia, US-remote),
plus the noise patterns found in a live run on 2026-08-11 (Remote
Poland/Spain/Australia leaking through, non-engineering titles passing).
Run with: python -m app.tests.test_pipeline
"""
import os
from app import filters
from app import dedup

SAMPLE_JOBS = [
    # Should PASS: junior DevOps, Baltimore, infra stack in the JD
    {"company": "Acme", "title": "Junior DevOps Engineer",
     "location": "Baltimore, MD", "url": "https://job-boards.greenhouse.io/acme/jobs/1111",
     "description": "Maintain Linux servers, Docker and GitHub Actions pipelines on AWS."},
    # Should FAIL: excluded title keyword (compliance)
    {"company": "Acme", "title": "Cloud Compliance Analyst",
     "location": "Remote - US", "url": "https://job-boards.greenhouse.io/acme/jobs/7788916003",
     "description": ""},
    # Should FAIL: seniority (senior) even though it's a priority DevOps title
    {"company": "Acme", "title": "Senior DevOps Engineer",
     "location": "Baltimore, MD", "url": "https://job-boards.greenhouse.io/acme/jobs/2222",
     "description": ""},
    # Should FAIL: real noise from 2026-08-11 run — "Remote Poland" used to leak
    # through the old "\bremote\b(?!.*\bus\b)" pattern.
    {"company": "Acme", "title": "Site Reliability Engineer",
     "location": "Remote Poland", "url": "https://job-boards.greenhouse.io/acme/jobs/7764109003",
     "description": ""},
    # Should FAIL: title has no target keyword at all
    {"company": "Acme", "title": "Marketing Operations Manager",
     "location": "Remote - US", "url": "https://job-boards.greenhouse.io/acme/jobs/9999",
     "description": ""},
    # Should FAIL: software-developer title is no longer a target
    {"company": "Acme", "title": "Software Engineer, Backend",
     "location": "Baltimore, MD", "url": "https://job-boards.greenhouse.io/acme/jobs/9998",
     "description": "Python and Linux."},
    # Should PASS: DC on-site Linux admin role
    {"company": "CapitolCo", "title": "Linux Systems Administrator",
     "location": "Washington, DC", "url": "https://jobs.lever.co/capitolco/1",
     "description": "RHEL patching, Bash scripting, Ansible."},
    # Should FAIL: requires a security clearance
    {"company": "FedCo", "title": "Systems Administrator",
     "location": "Fort Meade, MD", "url": "https://example.com/jobs/8888",
     "description": "Active TS/SCI with polygraph required. Linux administration."},
    # Should FAIL: mainframe-only stack dealbreaker
    {"company": "LegacyCo", "title": "Systems Administrator",
     "location": "Reston, VA", "url": "https://example.com/jobs/7777",
     "description": "Administer IBM z/OS mainframe and COBOL batch jobs."},
    # Should FAIL: 7+ years of experience required
    {"company": "BigCorp", "title": "Cloud Engineer",
     "location": "Remote, USA", "url": "https://example.com/jobs/6666",
     "description": "<ul><li>7+ years of experience with AWS</li><li>Terraform</li></ul>"},
    # Should PASS: accessibility track, certification only preferred
    {"company": "A11yCo", "title": "Digital Accessibility Analyst",
     "location": "Remote - US", "url": "https://example.com/jobs/5555",
     "description": "<li>Test with JAWS, NVDA and VoiceOver against WCAG 2.2</li><li>CPACC preferred</li>"},
    # Should FAIL: accessibility certification required
    {"company": "A11yCo", "title": "Accessibility Specialist",
     "location": "Remote - US", "url": "https://example.com/jobs/4444",
     "description": "<li>IAAP CPACC or WAS certification required</li><li>WCAG audits</li>"},
    # Duplicate of the first PASS entry by URL -> should be filtered by dedup on 2nd pass
    {"company": "Acme", "title": "Junior DevOps Engineer",
     "location": "Baltimore, MD", "url": "https://job-boards.greenhouse.io/acme/jobs/1111",
     "description": "Maintain Linux servers, Docker and GitHub Actions pipelines on AWS."},
    # Repost under a new URL, same company+title -> should be caught by company+title dedup
    {"company": "Acme", "title": "Junior DevOps Engineer",
     "location": "Baltimore, MD", "url": "https://job-boards.greenhouse.io/acme/jobs/3333-repost",
     "description": "Maintain Linux servers, Docker and GitHub Actions pipelines on AWS."},
    # Should PASS: NOC role in Northern Virginia, no description yet
    {"company": "NetOps Inc", "title": "NOC Technician II",
     "location": "Herndon, Fairfax County", "url": "https://jobs.lever.co/netops/2",
     "description": ""},
]

TEST_DB = "data/test_seen_jobs.sqlite3"


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    print("=== Filter results ===")
    filtered = []
    for job in SAMPLE_JOBS:
        ok = filters.passes_filters(job)
        print(f"{'PASS' if ok else 'DROP':5s} | {job['company']:14s} | {job['title'][:50]:50s} | {job['location']}")
        if ok:
            filtered.append(job)

    print(f"\n{len(filtered)}/{len(SAMPLE_JOBS)} passed title+location+stack filters\n")

    print("=== Dedup results (processing filtered jobs in order) ===")
    kept = []
    with dedup.connect(TEST_DB) as conn:
        for job in filtered:
            if dedup.is_new(conn, job):
                dedup.mark_seen(conn, job)
                kept.append(job)
                print(f"NEW  | {job['company']:14s} | {job['title'][:50]:50s} | {job['url']}")
            else:
                print(f"DUPE | {job['company']:14s} | {job['title'][:50]:50s} | {job['url']}")

    print(f"\nFinal candidates after filters+dedup: {len(kept)}")

    # Assertions to make this a real check, not just eyeballing.
    # PASS: Acme junior DevOps (x1 unique after dedup), CapitolCo Linux
    # admin, A11yCo analyst (cert only preferred), NetOps NOC technician.
    # DROP: compliance title, senior title, Remote Poland, marketing
    # title, software engineer title, clearance, mainframe stack, 7+ years,
    # required accessibility certification.
    assert len(filtered) == 6, f"expected 6 to pass all filters, got {len(filtered)}"
    assert len(kept) == 4, f"expected 4 unique candidates after dedup, got {len(kept)}"

    # Targeted unit checks.
    assert not filters.location_is_allowed("Remote Poland")
    assert not filters.location_is_allowed("Remote Spain")
    assert not filters.location_is_allowed("Remote Canada")
    assert not filters.location_is_allowed("Arlington, TX")
    assert not filters.location_is_allowed("Columbia, SC")
    assert not filters.location_is_allowed("Richmond, VA")
    assert filters.location_is_allowed("Towson, Maryland")
    assert filters.location_is_allowed("Arlington, VA")
    assert filters.location_is_allowed("United States")
    assert filters.location_is_allowed("United States of America")
    assert not filters.title_is_relevant("Marketing Operations Manager")
    assert not filters.title_is_relevant("Staff SRE")
    assert not filters.title_is_relevant("Sr. Linux Administrator")
    assert not filters.title_is_relevant("Systems Engineer III")
    assert not filters.title_is_relevant("Accessibility Program Manager")
    assert not filters.title_is_relevant("Civil Infrastructure Engineer")
    assert not filters.title_is_relevant("Sales Engineer")
    assert not filters.title_is_relevant("Transaction Monitoring Analyst")
    assert not filters.title_is_relevant("Data Scientist II, Infrastructure")
    assert not filters.title_is_relevant("Managing Consultant- Infrastructure Resilience")
    assert filters.title_is_relevant("Monitoring Technician")
    assert filters.title_is_relevant("Associate Cloud Support Engineer")
    assert filters.title_is_relevant("Site Reliability Engineer I")
    assert filters.title_is_relevant("Web Accessibility Tester")
    assert filters.requires_clearance("Systems Admin", "<li>Ability to obtain a Secret clearance</li>")
    assert not filters.requires_clearance("Systems Admin", "Clear communication with stakeholders.")
    assert filters.requires_clearance("AI Infrastructure Engineer - CLEARED", "")
    assert not filters.requires_clearance("Support Engineer", "Tickets are cleared within one business day.")
    assert not filters.requires_accessibility_cert("Familiarity with Trusted Tester methodology.")
    assert filters.requires_accessibility_cert("<li>Must hold IAAP CPACC</li><li>CPWA a plus</li>")
    assert not filters.requires_too_much_experience("2+ years of Linux experience required.")
    assert not filters.requires_too_much_experience("5+ years of experience preferred.")
    assert not filters.requires_too_much_experience("We were founded 10 years ago.")
    assert filters.requires_too_much_experience("Minimum 5 years of experience in IT operations.")
    assert filters.jd_stack_mismatch("Administer mainframe systems and COBOL jobs.")
    assert not filters.jd_stack_mismatch("Mainframe integration from our Linux platform.")
    assert not filters.jd_stack_mismatch("")  # no JD available -> don't reject on stack alone

    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()