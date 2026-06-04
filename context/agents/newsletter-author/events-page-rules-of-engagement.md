# Events Page — Rules of Engagement

This file tells you how to find Maryland Forward Party events for the newsletter's **Events** section. Follow it whenever you need to know what events are coming up.

## The core problem: do not scrape the rendered events page

The party website (marylandforwardparty.com) is a **Wix** site. The public-facing pages — `/events`, `/get-involved`, and `/get-involved/calendar` — render their event lists with JavaScript after the page loads. A normal fetch of those URLs returns a ~2.6 MB bundle with **no readable events in it**, and a summarizing fetch tool will only see the page title and report that the content was "truncated."

**Do not** try to read events off the rendered `/events` or `/get-involved` pages, and do not give up and guess. Use the structured sources below instead.

## The two reliable sources Wix publishes for crawlers

### 1. The event sitemap — the canonical list of every event

`https://www.marylandforwardparty.com/event-pages-sitemap.xml` lists the URL of every event. The slugs encode the date and time, so you can filter to a month without opening anything:

```
event-details/door-knocking-2026-06-13-07-00     ->  2026-06-13 07:00
event-details/code-forward-md-2026-06-15-19-00-1 ->  2026-06-15 19:00
```

Some one-off named events (festivals, forums, book clubs) have **no date in the slug** (e.g. `event-details/womens-candidate-forum`, `event-details/catonsville-pride-festival`). For those you must open the page and read the date from its embedded data (source #2).

There are sibling sitemaps from the same index (`/sitemap.xml`) worth knowing about: `dynamic-endorsed_...-sitemap.xml` and `dynamic-candidates_...-sitemap.xml` list endorsed-candidate pages, and `dynamic-legislative-actions_...-sitemap.xml` lists legislative pages. Use those the same way when you need that data.

### 2. The page's embedded JSON-LD — the exact details of one event

Every event page embeds a schema.org block in its **raw HTML** (before any JavaScript runs):

```html
<script type="application/ld+json"> { "@type":"Event", "startDate":"...", "endDate":"...", "location":{...}, "description":"..." } </script>
```

Fetch the page's raw HTML and parse that JSON directly. It gives you the exact `startDate`, `endDate`, `location` (name + street address), and `description` — fully reliable and citeable. This is how you get details for both dated and undated events.

## Use the script

`fetch_events.py` (in this directory) automates the whole pipeline: read the sitemap, filter to the target month, drop committee meetings, open each event's JSON-LD, and print a clean, chronologically sorted list. It uses only the Python standard library — no installs needed.

```
python3 fetch_events.py 2026-06           # public events for June 2026, human-readable
python3 fetch_events.py 2026-06 --json    # same data as JSON, for further processing
python3 fetch_events.py 2026-06 --all     # also include committee/internal meetings
```

Run this first. Only fall back to fetching the sitemap and event pages by hand if the script fails (for example, if Wix changes its URL structure — in which case update the script and this file).

## What to include and exclude

- **Look 6–8 weeks ahead** from the date you are writing, per the main instructions. You can run the script for the current month and the next month and combine the results.
- **Exclude committee and internal meetings.** The newsletter audience is people who are not yet party members. The script already filters these out by default (legislative-affairs, candidates-committee, community-action, communications, volunteer-coordination, county-leads calls, open board meetings, and internal team meetings). If you spot a new internal-meeting series in the output, add its slug pattern to `COMMITTEE_PATTERNS` in the script.
- **Recurring series** (door-knocking weekends, Code Forward every other Monday) can be summarized as a recurring entry rather than listed date-by-date if that reads better — use your judgment.
- **Never invent or guess** a date, time, or location. If the JSON-LD does not have it, leave it out. Everything in the Events section must come from the sitemap/JSON-LD or from notes the user provided.
- **Do not list past events as upcoming.** Check the current date and drop anything that has already happened.
