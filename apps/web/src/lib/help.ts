/**
 * The in-app how-to guides.
 *
 * Organised the Diátaxis way (https://diataxis.fr), which most documentation sites
 * follow: each section first *explains* what it is for, then gives *how-to guides*
 * — one goal each, titled "How to …", with what you need and numbered steps. A
 * guide never mixes two goals, so a member looking for one task finds exactly it.
 *
 * Text is written against the screens as they are: button and field names appear
 * in **bold** exactly as on the page. A change to a page's labels is a change here.
 */

import type { NavIcon } from "@/lib/navigation";
import { describeStatus, statusGroup, type StatusKind } from "@/lib/statuses";

export interface HowTo {
  /** "How to …", one goal. */
  title: string;
  /** Who can do it, or what must already be true. */
  needs?: string;
  steps: string[];
  /** A caution or tip shown after the steps. */
  note?: string;
}

export interface HelpSection {
  id: string;
  title: string;
  /** What the section is for: one or two paragraphs. */
  about: string[];
  howTos: HowTo[];
  /**
   * Status groups shown in this section, each rendered as a table of what every
   * value means. Read from the same glossary as the info button beside a chip.
   */
  statuses?: StatusKind[];
}

export interface HelpTopic {
  id: string;
  title: string;
  /** The page this topic documents; absent for the getting-started topic. */
  href?: string;
  icon: NavIcon | "help";
  /** Capabilities that open the page; empty means every signed-in member. */
  anyOf: readonly string[];
  /** Who it is for, in words. */
  audience: string;
  summary: string;
  sections: HelpSection[];
}

const STEWARD = "Data stewards and admins.";

export const HELP_TOPICS: readonly HelpTopic[] = [
  {
    id: "getting-started",
    title: "Getting started",
    icon: "help",
    anyOf: [],
    audience: "Everyone.",
    summary: "Signing in, joining from an invitation, finding your way, and what each role can do.",
    sections: [
      {
        id: "signing-in",
        title: "Signing in",
        about: [
          "The Lab AI Assistant is for lab members only: there is no public access and no self-registration. Every account comes from an administrator's invitation, and every page except sign-in and the invitation page needs you to be signed in.",
          "If you open a page while signed out, you are sent to sign in and brought back to that page afterwards. A session lasts a working day; signing out ends it on every device at once.",
        ],
        howTos: [
          {
            title: "How to sign in",
            needs: "An account, created by accepting an invitation.",
            steps: [
              "Open the app's address. If you are not signed in, the **Sign in** page opens.",
              "Enter your **Email** and **Password**.",
              "Select **Sign in**. You land on the page you were going to, or on **Ask**.",
            ],
            note: "If sign-in says your access is suspended, your password was right but an admin has suspended your membership. Ask an admin to reinstate it.",
          },
          {
            title: "How to sign out",
            steps: ["Select **Sign out** at the bottom of the sidebar."],
            note: "Signing out ends every session you have, not only the one in this browser.",
          },
        ],
      },
      {
        id: "invitations",
        title: "Accepting an invitation",
        about: [
          "An admin invites you by sending a link. The link names the workspace and the role you are joining as, works once, and expires (after seven days unless the admin chose otherwise). If the admin invites you again, the earlier link stops working.",
          "If you already have an account with the same email, you join with your existing password; otherwise you choose one.",
        ],
        howTos: [
          {
            title: "How to join with a new account",
            needs: "An invitation link sent to your address.",
            steps: [
              "Open the link. The page says which workspace and role you are invited to.",
              "Optionally enter your **Full name**.",
              "Enter a **Password** of at least 12 characters, and repeat it.",
              "Select **Create account and join**. You arrive signed in.",
            ],
          },
          {
            title: "How to join with an account you already have",
            steps: [
              "Open the link.",
              "Enter **Your existing password**.",
              "Select **Join**.",
            ],
            note: "If the page says the invitation is not valid, it has expired, been used, or been replaced. Ask an admin for a new link.",
          },
        ],
      },
      {
        id: "sidebar",
        title: "Finding your way: the sidebar",
        about: [
          "The sidebar on the left lists the pages your role can use, grouped into Research, Curation, Publishing, Administration and Help. After you sign in, only the group holding the current page is open; the groups you open stay open in this browser.",
          "You can hide the sidebar down to a strip of icons to make room for wide tables. Hover over an icon, or move to it with the keyboard, to see its name.",
        ],
        howTos: [
          {
            title: "How to open or close a group",
            steps: ["Select the group's name (for example **Curation**). Select it again to close it."],
          },
          {
            title: "How to hide or show the sidebar",
            steps: [
              "Select the square button at the top of the sidebar (**Hide navigation**).",
              "To bring it back, select the same button (**Show navigation**).",
            ],
          },
        ],
      },
      {
        id: "live-updates",
        title: "Lists that update themselves",
        about: [
          "Work such as searching the sources, fetching full texts, indexing a release, extraction and evaluation runs happens in the background. Each list that shows such work has an indicator in its header.",
          "**Updating live**, with a pulsing dot, means something in the list is still in progress, or you have just changed something: the list is read again every few seconds until the work settles. Otherwise the header says when the list was last updated. Reading pauses while the browser tab is hidden and resumes when you return.",
        ],
        howTos: [
          {
            title: "How to update a list now",
            steps: ["Select **Refresh** next to the indicator in the list's header."],
          },
        ],
      },
      {
        id: "statuses",
        title: "What a status means",
        statuses: ["flag"],
        about: [
          "Statuses appear as small coloured labels, such as **running**, **degraded_partial** or **awaiting_user**. The label is the status's exact name, so it matches what the server records. Colour gives the gist: green is done, amber needs attention or is in progress, red has failed.",
          "Beside each status is an **ⓘ** button. Select it to see a short description of what that status means there; the same word can mean different things in different places. Each page's guide also has a table of every status it can show, under **Statuses**.",
        ],
        howTos: [
          {
            title: "How to find out what a status means",
            steps: [
              "Select the **ⓘ** button beside the status.",
              "Read the description. Select the button again, press Escape or click elsewhere to close it.",
            ],
            note: "To see every status a page can show, open its guide and scroll to **Statuses**.",
          },
        ],
      },
      {
        id: "roles",
        title: "Roles and what they can do",
        about: [
          "Your role decides which pages you see and what you can change there. **Researchers** ask questions and read analytics. **Reviewers** also review extracted claims, grade evaluation answers and decide identity conflicts. **Data stewards** also build collections, run extraction, make releases and run evaluations. **Admins** can do everything, including managing members and reading operations.",
          "Hiding a page is a courtesy: the server checks every action against your role, so a button you can see but may not use is refused with an explanation.",
        ],
        howTos: [
          {
            title: "How to see your role",
            steps: ["Look at the bottom of the sidebar: your email and role are shown above **Sign out**."],
          },
        ],
      },
    ],
  },
  {
    id: "ask",
    title: "Ask",
    href: "/ask",
    icon: "ask",
    anyOf: ["ask"],
    audience: "Everyone with the researcher role or above.",
    summary: "Ask the published corpus a question and check every citation against its source.",
    sections: [
      {
        id: "asking",
        title: "Asking a question",
        about: [
          "Ask answers questions from the published corpus only: the release currently being served. It first retrieves the passages most relevant to your question, shows them as **Sources**, and then writes an answer that cites them.",
          "Every citation is a handle such as [E1a2b3c4d5] that opens the exact passage it came from. A citation the system cannot verify is shown as flagged rather than hidden, so you always know which claims are backed.",
        ],
        howTos: [
          {
            title: "How to ask a question",
            steps: [
              "Type your question in the box at the bottom of the conversation.",
              "Select **Ask**. The sources appear on the right as soon as retrieval finishes; the answer follows word by word.",
              "To ask a follow-up, type it in the same box. The conversation keeps its context.",
            ],
            note: "Ask about what the papers report, as specifically as you can: strain, product, condition. The answer will say when the corpus does not cover something.",
          },
          {
            title: "How to check a citation",
            steps: [
              "Select a citation handle in the answer.",
              "Read the passage under **Evidence in context**, below the sources.",
            ],
          },
        ],
      },
      {
        id: "sessions",
        title: "Conversations",
        about: [
          "Each conversation is a session, listed under **Sessions** on the left and named after its first question. Sessions are private to you, except that admins of the workspace can read them (see below).",
        ],
        howTos: [
          {
            title: "How to start a new conversation",
            steps: ["Select **New** at the top of **Sessions**, then ask your question."],
          },
          {
            title: "How to rename or delete a conversation",
            steps: [
              "Find it under **Sessions**.",
              "Select **rename** and type the new title, or select **delete**.",
            ],
            note: "Deleting hides the conversation from you; it does not give back the tokens it used.",
          },
        ],
      },
      {
        id: "feedback",
        title: "Telling us whether an answer helped",
        about: [
          "Under each finished answer you can mark it **useful** or **not useful**. Feedback goes to the people who maintain the corpus and the evaluation benchmarks, and is how weak areas are found.",
        ],
        howTos: [
          {
            title: "How to give feedback on an answer",
            steps: ["Under the answer, select **useful** or **not useful**."],
          },
        ],
      },
      {
        id: "allowance",
        title: "Your token allowance and who can read your chats",
        about: [
          "Each answer uses tokens (the model's unit of text), and each member has an allowance in total. The line under the question box shows how much you have used. When it is used up, asking is paused until an admin raises your limit.",
          "If the line says admins can read your conversations, this workspace allows admins to open members' chats. Every such read is recorded in the audit log with the admin's name.",
        ],
        howTos: [
          {
            title: "How to see how much of your allowance is left",
            steps: ["Look under the question box on **Ask**: it shows tokens used out of your limit."],
            note: "To get more, ask an admin: they set limits on the **Members** page.",
          },
        ],
      },
    ],
  },
  {
    id: "analytics",
    title: "Analytics",
    href: "/analytics",
    icon: "analytics",
    anyOf: ["view_analytics"],
    audience: "Everyone with the researcher role or above.",
    summary: "What the served release contains: counts, publication years, venues, subjects and sources.",
    sections: [
      {
        id: "counts",
        title: "Release counts",
        about: [
          "The tiles at the top describe the release being served: **Works** (papers), **Renditions** (parsed full texts), **Claims** (reviewed extracted values), and **Chunks** (searchable passages). Counts follow the same access rules as Ask, so they show what you can see.",
          "**Withheld by policy** is the amount of material you may not open. Stewards see the number; others see that some is withheld, which is what they need to ask for access.",
        ],
        howTos: [
          {
            title: "How to see what the current release contains",
            steps: ["Open **Analytics**. The release name and its counts are at the top."],
          },
        ],
      },
      {
        id: "landscape",
        title: "Years, venues, subjects and sources",
        about: [
          "**Publication years** shows how the corpus spreads over time, **Venues** the journals it draws on, **Subject terms** the subjects the sources assigned, and **Source contribution** how many papers each search source found, including those only it found.",
          "Use them to judge coverage: a gap in years or a missing journal is a reason to widen a collection's scope.",
        ],
        howTos: [
          {
            title: "How to check which sources a collection depends on",
            steps: [
              "Open **Analytics** and scroll to **Source contribution**.",
              "Compare each source's unique contribution: a source that adds nothing unique could be dropped from the scope; one that adds many is essential.",
            ],
          },
        ],
      },
    ],
  },
  {
    id: "collections",
    title: "Collections",
    href: "/collections",
    icon: "collections",
    anyOf: ["manage_scope", "run_discovery", "decide_identity", "decide_rights", "acquire"],
    audience: "Data stewards and admins; reviewers can decide identity conflicts.",
    summary: "Define what to collect, search the sources, decide what belongs, and get the full texts.",
    sections: [
      {
        id: "scopes",
        statuses: ["approval"],
        title: "Scopes",
        about: [
          "A scope says what a collection is for and where to search: a scientific objective, inclusion and exclusion criteria, and a query for each source (PubMed, Europe PMC, Crossref, OpenAlex, DataCite) in that source's own syntax.",
          "A scope starts as a draft. Its gate checks that it is complete enough to run; once approved, discovery can search with it. Until then a draft can be corrected, saved as a new version, or deleted. Approved scopes cannot be changed: they are a record of exactly what was searched.",
        ],
        howTos: [
          {
            title: "How to create a scope",
            needs: STEWARD,
            steps: [
              "On **Collections**, select **New scope**.",
              "Enter a **Name** and the **Scientific objective**.",
              "List the **Inclusion criteria** and, optionally, the **Exclusion criteria**, one per line.",
              "Under **Intended uses**, say what the collection is for, one per line (for example literature search).",
              "Under sources, tick each source to search and type its query (or write them with the organism lookup, below).",
              "Select **Save as draft**.",
            ],
            note: "The gate needs inclusion criteria, intended uses and a query that compiles for every ticked source.",
          },
          {
            title: "How to fix a scope whose gate fails",
            needs: STEWARD,
            steps: [
              "Select **Edit** on the draft. The form opens with its values.",
              "Correct what the gate listed.",
              "Select **Save changes** to correct this version, or **Save as v2** (the next version number) to keep it unchanged and add a new draft.",
              "Select **Check gate** again, then **Approve**.",
            ],
            note: "Only unapproved versions can be edited. An approved scope is the record of what was searched: change it by editing and saving a new version from a draft.",
          },
          {
            title: "How to delete a draft scope",
            needs: STEWARD,
            steps: ["Select **Delete** on the draft and confirm."],
            note: "Only unapproved versions can be deleted. The deletion is recorded in the audit log.",
          },
          {
            title: "How to approve a scope",
            needs: STEWARD,
            steps: [
              "Select **Check gate** on the scope. A passing gate says how many sources compiled; a failing one lists what is missing.",
              "When it passes, select **Approve**.",
            ],
          },
        ],
      },
      {
        id: "query-lookup",
        title: "Writing queries from an organism and a product",
        about: [
          "The literature uses many names for one organism: papers on *Yarrowia lipolytica* were long published as *Candida lipolytica*. The lookup finds every name NCBI Taxonomy lists for an organism, and every name PubChem lists for a product (without registry codes), then writes each source's query for you.",
          "The queries are a starting point: they fill the fields of the scope form, where you can edit them before saving.",
        ],
        howTos: [
          {
            title: "How to write a scope's queries from an organism",
            needs: STEWARD,
            steps: [
              "In the new-scope form, type at least three letters of the organism under **Organism**.",
              "Pick the right organism from the suggestions. Its names appear as ticked boxes.",
              "Untick any name the literature you want does not use.",
              "Optionally do the same under **Product**.",
              "Select **Write the source queries**. Each source's query is filled in.",
            ],
            note: "If the lookup service does not answer, write the queries by hand: nothing else depends on it.",
          },
        ],
      },
      {
        id: "discovery",
        statuses: ["discovery_run", "source_outcome"],
        title: "Discovery runs",
        about: [
          "A discovery run searches every source of an approved scope and records what each one returned. While it runs, each source shows its outcome and how many records it has seen.",
          "Freezing a run turns what it found into candidates for review. If a required source did not finish, the run is degraded: you can freeze it anyway, and the gap is recorded.",
        ],
        howTos: [
          {
            title: "How to search the sources",
            needs: STEWARD,
            steps: [
              "On an approved scope, select **Start discovery**.",
              "Follow the run under **Discovery runs**: each source's outcome, records seen and declared total.",
            ],
          },
          {
            title: "How to freeze a run",
            needs: STEWARD,
            steps: [
              "When the sources have answered, select **Freeze** on the run.",
              "If the run is degraded, select **Freeze, accepting the gap** and confirm.",
            ],
            note: "Only freeze a degraded run when you know why a source fell short: the release will record that it was accepted.",
          },
        ],
      },
      {
        id: "identity",
        statuses: ["duplicate_reason", "identity"],
        title: "Identity conflicts and duplicate proposals",
        about: [
          "Different sources often describe the same paper. They are merged automatically only on strong identifiers (DOI, PMID, PMCID). When two records look like the same paper by title alone, they are proposed here for a person to decide, because titles alone are not reliable.",
          "Papers whose identifiers contradict each other are listed as conflicts and cannot enter a release until resolved.",
        ],
        howTos: [
          {
            title: "How to merge two records of the same paper",
            needs: "Reviewers, data stewards and admins.",
            steps: [
              "Read the proposal under **Identity conflicts and duplicate proposals**: its similarity and reason.",
              "If they are the same paper, select **Merge**.",
            ],
          },
        ],
      },
      {
        id: "candidates",
        statuses: ["relevance", "retraction"],
        title: "Candidates",
        about: [
          "Candidates are the papers discovery found, each with the fields the sources agreed on. Including a candidate puts it in the collection and plans the acquisition of its full text; excluding it records that it is out of scope.",
        ],
        howTos: [
          {
            title: "How to decide whether a paper belongs",
            needs: STEWARD,
            steps: [
              "Under **Candidates**, read the paper's details. Open **which sources asserted these fields** to see where each value came from.",
              "Select **Include** or **Exclude**.",
            ],
          },
        ],
      },
      {
        id: "acquisition",
        statuses: ["acquisition", "acquisition_outcome"],
        title: "Acquisition queue",
        about: [
          "Every included paper gets an acquisition plan: the routes tried to fetch its full text, open repositories first. When no permitted route returns it, the plan waits for a person, who can upload a copy the lab is entitled to.",
          "An uploaded copy goes through the same integrity, malware, identity and rights checks as an automatic download, and the paper then continues to parsing.",
        ],
        howTos: [
          {
            title: "How to upload a paper the system could not fetch",
            needs: STEWARD,
            steps: [
              "Under **Acquisition queue**, find a plan marked assisted or failed and select **Upload an authorised copy**.",
              "Check the **Access basis** (for example an institutional subscription).",
              "In **Attestation**, say how the copy was obtained.",
              "Choose the file and select **Upload**.",
            ],
          },
        ],
      },
      {
        id: "bulk-import",
        statuses: ["bulk_import"],
        title: "Importing many PDFs",
        about: [
          "When you have many papers as PDFs, drop them together. Each file is matched to the acquisition waiting for it by a DOI, PMID or PMCID in its file name or on its first page — never by title — and imported through the same checks as a single upload.",
          "A file that matches nothing, or could belong to more than one paper, is listed for you to assign by hand.",
        ],
        howTos: [
          {
            title: "How to import a folder of PDFs",
            needs: STEWARD,
            steps: [
              "Under **Import many PDFs**, drop the files or choose them.",
              "Fill in the **Access basis** and an **Attestation** that applies to every file.",
              "Select **Check matches** to see what would happen. Nothing is stored yet.",
              "Select **Import**.",
              "For any file marked unmatched or ambiguous, choose its acquisition from **Assign to a waiting acquisition**.",
            ],
            note: "Naming files after their DOI (with _ instead of /, for example 10.1234_abc.pdf) gives the surest match.",
          },
        ],
      },
    ],
  },
  {
    id: "review",
    title: "Review",
    href: "/review",
    icon: "review",
    anyOf: ["review_claims", "review_parse"],
    audience: "Reviewers, data stewards and admins.",
    summary: "Check each value extraction proposed against its evidence, and accept, correct or reject it.",
    sections: [
      {
        id: "claim-review",
        statuses: ["claim_validation", "abstention", "proposal_source"],
        title: "Reviewing extracted claims",
        about: [
          "Extraction proposes values — a lipid yield, a temperature — each with the exact passage it came from and any issues automatic validation found. Nothing proposed is a finding until a person reviews it: only reviewed claims enter a release.",
          "Accept a value only when its evidence supports it exactly, including the unit and the basis. A proposal that failed validation cannot be accepted; correct it or reject it.",
        ],
        howTos: [
          {
            title: "How to review a proposed value",
            needs: "Reviewers, data stewards and admins.",
            steps: [
              "Read the value, its evidence passage and any validation issues.",
              "If it is right, select **Accept**.",
              "If the evidence supports a different value, type it in the **corrected value** field and select **Correct**.",
              "If it is wrong, select **Reject**; if you cannot decide yet, select **Defer**.",
            ],
            note: "Untick **show proposals that failed validation** to see only the proposals that can be accepted.",
          },
        ],
      },
      {
        id: "correction-rate",
        title: "Correction rate by predicate",
        about: [
          "The table at the bottom shows, for each kind of value, how often reviewers had to correct what extraction proposed. A high rate marks a value that deserves a second reviewer or a clearer template.",
        ],
        howTos: [
          {
            title: "How to find the values extraction gets wrong most",
            steps: ["Scroll to **Correction rate by predicate**. The most-corrected values are at the top."],
          },
        ],
      },
    ],
  },
  {
    id: "datasheets",
    title: "Datasheets",
    href: "/datasheets",
    icon: "datasheets",
    anyOf: ["run_extraction"],
    audience: STEWARD,
    summary: "Say what to extract, run extraction, estimate its cost, and download the working sheet.",
    sections: [
      {
        id: "templates",
        statuses: ["approval"],
        title: "Templates",
        about: [
          "A template says what to extract from each paper: its targets (for example strain and lipid yield), what kind of value each is, its unit, and where it usually appears. Nothing is extracted until a template is approved.",
          "A registered version never changes. To change a template, register it again with a new semantic_version and approve that one; claims already extracted keep the version that produced them.",
        ],
        howTos: [
          {
            title: "How to add and approve a template",
            needs: STEWARD,
            steps: [
              "Select **Add template**.",
              "Upload a YAML or JSON file, paste it, or select **Start from an example**.",
              "Select **Register**. The template is saved as a draft.",
              "Select **Approve** on it. Extraction now uses it.",
            ],
          },
        ],
      },
      {
        id: "extract",
        statuses: ["extraction_run"],
        title: "Extracting, and what it will cost",
        about: [
          "Extraction runs by itself after each paper is parsed. **Run** covers the papers parsed before the template was approved; **Re-run all** extracts every paper again, for example after a model change. Unchanged inputs reuse their earlier result at no cost.",
          "**Estimate** says how many papers would be extracted and roughly how many tokens it would use, priced at the configured rates.",
        ],
        howTos: [
          {
            title: "How to estimate and run extraction",
            needs: STEWARD,
            steps: [
              "Under **Extract**, choose the **Template**.",
              "Select **Estimate** and read the number of papers and tokens.",
              "Select **Run**.",
              "Follow the run under **Runs**; select **Cancel** to stop the tasks not yet started.",
            ],
          },
        ],
      },
      {
        id: "working-sheet",
        title: "The working sheet",
        about: [
          "The working sheet shows every current proposal for the chosen template, one row per paper and experiment, reviewed or not. It is for the people building the datasheet: unreviewed values are proposals, not findings.",
          "The citable datasheet is the wide-form spreadsheet in a release's export package (see Releases).",
        ],
        howTos: [
          {
            title: "How to download the working sheet",
            needs: STEWARD,
            steps: [
              "Choose the **Template** under **Extract**.",
              "Under **Working sheet**, select **Wide CSV** (one row per experiment) or **Long CSV** (one row per value).",
            ],
          },
        ],
      },
    ],
  },
  {
    id: "answer-reviews",
    title: "Answer reviews",
    href: "/answer-reviews",
    icon: "answer-reviews",
    anyOf: ["review_claims"],
    audience: "Reviewers, data stewards and admins.",
    summary: "Grade the answers an evaluation run produced, on the review form.",
    sections: [
      {
        id: "grading",
        statuses: ["answer_review"],
        title: "Grading assigned answers",
        about: [
          "Automatic metrics measure whether citations are valid and numbers are backed; they cannot judge whether an answer is right. When an evaluator assigns you answers from an evaluation run, they appear here with the question, the answer and what was measured.",
          "Your grades are kept beside the automatic metrics and summarised for the run. You can update a review after submitting it.",
        ],
        howTos: [
          {
            title: "How to grade an answer",
            needs: "Answers assigned to you.",
            steps: [
              "Read the **Question** and the **Answer**.",
              "Give each score from 1 to 5; **correctness** is required.",
              "Tick any **Issues** you saw, such as hallucination or corpus gap.",
              "Optionally give **Your confidence**, a **Comment** and a **Suggested answer**.",
              "Select **Submit review**.",
            ],
          },
        ],
      },
    ],
  },
  {
    id: "releases",
    title: "Releases",
    href: "/releases",
    icon: "releases",
    anyOf: ["manage_release", "approve_release", "export"],
    audience: STEWARD,
    summary: "Turn reviewed material into a published release, roll back, and export datasheets.",
    sections: [
      {
        id: "making",
        statuses: ["release"],
        title: "Drafting, validating and approving",
        about: [
          "A release is a frozen, citable snapshot of the collection: its papers, their parsed texts and the reviewed claims. Ask answers only from the release being served, so nothing changes under a reader's feet.",
          "A draft is validated against its gates (for example, every claim reviewed); approving it builds the search index.",
        ],
        howTos: [
          {
            title: "How to make a new release",
            needs: STEWARD,
            steps: [
              "Select **Draft from approved material**.",
              "Select **Validate** on the draft and read its gate results.",
              "When the gates pass, select **Approve and index**.",
            ],
          },
        ],
      },
      {
        id: "publishing",
        title: "Publishing and required benchmarks",
        about: [
          "Publishing makes a release the one Ask and Analytics serve. It is offered once the release is approved, its gates pass and its index is ready.",
          "If benchmarks are required for publishing (set on Evaluation), the release must have a passing run of each. An admin can publish anyway by giving a reason, which is recorded in the audit log.",
        ],
        howTos: [
          {
            title: "How to publish a release",
            needs: STEWARD,
            steps: [
              "Check the required-benchmark chips on the release: each should say pass.",
              "Select **Publish**.",
            ],
            note: "If publishing is refused for a missing benchmark, run that benchmark on Evaluation. An admin can instead select **Publish anyway (admin)** and give a reason; for anyone else it is refused.",
          },
          {
            title: "How to roll back to the previous release",
            needs: STEWARD,
            steps: ["Under **Serving aliases**, select **Roll back**."],
          },
        ],
      },
      {
        id: "exports",
        title: "Exports and the datasheet",
        about: [
          "An export package holds a release's papers, claims and evidence in documented files, including the wide-form datasheet as a spreadsheet. It is the citable form of the datasheet.",
        ],
        howTos: [
          {
            title: "How to download a release's datasheet",
            needs: "Members who may export.",
            steps: [
              "Select **Build export** on the release.",
              "Select **Download everything (.zip)**, or a single file such as **wide-form.xlsx**.",
            ],
          },
        ],
      },
    ],
  },
  {
    id: "evaluation",
    title: "Evaluation",
    href: "/evaluation",
    icon: "evaluation",
    anyOf: ["run_evaluation"],
    audience: STEWARD,
    summary: "Measure a release with benchmarks, compare runs, gate publishing, and review answers by hand.",
    sections: [
      {
        id: "benchmarks",
        statuses: ["benchmark_kind", "benchmark_sensitivity"],
        title: "Benchmarks",
        about: [
          "A benchmark is a set of questions with what a good result looks like: for retrieval, the papers that should be found; for generation, questions whose answers are checked; for policy, probes that must not see restricted material.",
          "A registered version never changes. To correct a gold answer, edit the benchmark and register the edit as the next version.",
        ],
        howTos: [
          {
            title: "How to write a benchmark",
            needs: STEWARD,
            steps: [
              "Under **Benchmarks**, select **Write one**.",
              "Give it a **Benchmark id**, a **Kind** and a **Description**.",
              "For each question, give a **Case id**, the **Question** and, for retrieval, the relevant papers (doi:…, pmid:…), one per line. Select **Add a question** for more.",
              "Set the **Thresholds** a run must reach.",
              "Select **Register**.",
            ],
            note: "To load a file instead, select **Add benchmark** and upload or paste it.",
          },
          {
            title: "How to change a benchmark",
            needs: STEWARD,
            steps: [
              "Select **Edit as new version** on the benchmark. The next version number is filled in.",
              "Make your changes and select **Register**.",
            ],
          },
        ],
      },
      {
        id: "runs",
        statuses: ["evaluation_run"],
        title: "Running and comparing",
        about: [
          "A run measures one release with one benchmark, on the evaluation worker and with your access. Its verdict is passed or failed gates, with every metric and, under **Details**, each question's result.",
          "Comparing two runs of the same benchmark shows how each metric changed and which questions moved, which is how you see whether a new release is better.",
        ],
        howTos: [
          {
            title: "How to evaluate a release",
            needs: "The release approved and indexed.",
            steps: [
              "Under **Run an evaluation**, choose the **Benchmark** and the **Release**.",
              "Select **Run**. The run is queued and the page updates when it finishes.",
              "Select **Details** on the run to see its gates and per-question results; **Cancel** stops a queued run.",
            ],
          },
          {
            title: "How to compare two runs",
            steps: [
              "Tick the two finished runs.",
              "Select **Compare selected**. Read the change in each metric and the questions that changed.",
            ],
          },
        ],
      },
      {
        id: "gate",
        title: "Requiring a benchmark for publishing",
        about: [
          "Marking a benchmark as required means no release can be published without a passing run of it. It is how a quality bar becomes a rule rather than a habit.",
        ],
        howTos: [
          {
            title: "How to require a benchmark before publishing",
            needs: STEWARD,
            steps: [
              "Under **Benchmarks**, select **Require for publishing** on the benchmark version.",
              "To lift the requirement, select **Stop requiring**.",
            ],
          },
        ],
      },
      {
        id: "human-review",
        title: "Human review of answers",
        about: [
          "For a generation run, you can have reviewers grade a sample of the answers. The sample is stable for a run, so asking again assigns the next answers rather than the same ones. The run's details then show how many are graded, the average of each score and the issues flagged.",
        ],
        howTos: [
          {
            title: "How to have answers graded",
            needs: "A finished generation run, and members with the reviewer role.",
            steps: [
              "Select **Details** on the run.",
              "Under **Human review**, tick the reviewers.",
              "Set how many answers to sample and select **Assign answers**.",
              "Reviewers grade them on **Answer reviews**.",
            ],
          },
        ],
      },
    ],
  },
  {
    id: "members",
    title: "Members",
    href: "/members",
    icon: "members",
    anyOf: ["manage_members"],
    audience: "Admins.",
    summary: "Invite people, set roles and token limits, suspend access, and read members' chats.",
    sections: [
      {
        id: "inviting",
        statuses: ["invitation"],
        title: "Inviting people",
        about: [
          "People join by invitation only. Creating an invitation gives you a link to send them yourself — the app does not send email. The link is shown once; if it is lost, invite the address again, which also cancels the earlier link.",
        ],
        howTos: [
          {
            title: "How to invite someone",
            needs: "Admins.",
            steps: [
              "Under **Invite someone**, enter their **Email**, choose a **Role**, and set **Valid for (days)**.",
              "Select **Create link**.",
              "Select **Copy link** and send it to them.",
            ],
            note: "Open invitations are listed under **Invitations**; select **Revoke** to cancel one.",
          },
        ],
      },
      {
        id: "managing",
        statuses: ["membership"],
        title: "Roles, suspension and signing out",
        about: [
          "Each member's role is chosen from the list in their row and takes effect on their next action. Suspending someone signs them out everywhere at once and stops them signing in until reinstated; **Sign out everywhere** ends their sessions without changing their access.",
          "Nobody can change their own role or suspend themselves, and only an admin can make someone an admin, so a workspace always keeps an admin.",
        ],
        howTos: [
          {
            title: "How to change a member's role",
            needs: "Admins.",
            steps: ["In the member's row, choose the new role from the **Role** list."],
          },
          {
            title: "How to suspend or reinstate a member",
            needs: "Admins.",
            steps: [
              "Select **Suspend** in their row and confirm.",
              "To restore access, select **Reinstate**.",
            ],
          },
        ],
      },
      {
        id: "usage",
        title: "Usage and token limits",
        about: [
          "The tiles show the workspace at a glance: active members, questions asked, tokens used, and members at their limit. Each row shows the member's tokens used against their limit.",
          "Every member has the installation's default limit unless you set one for them.",
        ],
        howTos: [
          {
            title: "How to change a member's token limit",
            needs: "Admins.",
            steps: [
              "Select **Change limit** in their row.",
              "Enter the new limit and select **Save**, or select **Use default** to return them to the default.",
            ],
          },
        ],
      },
      {
        id: "reading-chats",
        title: "Reading a member's conversations",
        about: [
          "Where the installation allows it, admins can read members' conversations, including deleted ones. Members are told so on the Ask page, and every list and every conversation you open is recorded in the audit log under your name.",
        ],
        howTos: [
          {
            title: "How to read a member's conversations",
            needs: "Admins, where the installation allows it.",
            steps: [
              "Select **Read chats** in the member's row.",
              "Choose a conversation from the list to read it.",
              "Select **Close** when you are done.",
            ],
          },
        ],
      },
    ],
  },
  {
    id: "operations",
    title: "Operations",
    href: "/operations",
    icon: "operations",
    anyOf: ["view_operations"],
    audience: "Admins.",
    summary: "Watch the task queue, retry failed tasks, and read the audit log.",
    sections: [
      {
        id: "queue",
        statuses: ["task"],
        title: "The task queue",
        about: [
          "Discovery, acquisition, parsing, extraction, indexing and evaluation all run as tasks on background workers. The tiles count tasks by state, **Waiting longest, by stage** shows where work is backing up, and **Waiting for a person** lists tasks that need a decision. The page refreshes itself.",
          "A task that fails is retried automatically; one that keeps failing becomes a dead letter and stops, so a broken input never loops forever.",
        ],
        howTos: [
          {
            title: "How to retry a failed task",
            needs: "Admins.",
            steps: [
              "Under **Dead letters**, read the task's last error.",
              "Once the cause is fixed, select **Retry**.",
            ],
            note: "Retrying without fixing the cause only fails again: check the error first.",
          },
        ],
      },
      {
        id: "audit",
        statuses: ["audit_action"],
        title: "The audit log",
        about: [
          "Every decision that matters — sign-ins, membership changes, reviews, releases, overrides, reads of members' chats — is written to a tamper-evident audit log. The latest 100 events are shown here with what was done and to what.",
        ],
        howTos: [
          {
            title: "How to see who changed something",
            needs: "Admins.",
            steps: ["Scroll to **Audit log (latest 100)** and find the event by its action and subject."],
          },
        ],
      },
    ],
  },
];

/** A topic by id, for the topic page. */
export function helpTopic(id: string): HelpTopic | undefined {
  return HELP_TOPICS.find((topic) => topic.id === id);
}

/** The topic documenting a page, for that page's "how-to guide" link. */
export function helpFor(href: string): HelpTopic | undefined {
  return HELP_TOPICS.find((topic) => topic.href === href);
}

export interface HelpMatch {
  topic: HelpTopic;
  section: HelpSection;
  howTo?: HowTo;
}

/**
 * Every section or guide whose words contain all the query's words, best first: a
 * match in a guide's title outranks one in its steps, which outranks the prose.
 */
export function searchHelp(query: string): HelpMatch[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const has = (text: string, word: string) => text.toLowerCase().includes(word);
  const matches = (text: string) => words.every((word) => has(text, word));
  const scored: { match: HelpMatch; score: number; order: number }[] = [];
  for (const topic of HELP_TOPICS) {
    for (const section of topic.sections) {
      let found = false;
      for (const howTo of section.howTos) {
        if (!matches([howTo.title, ...howTo.steps, howTo.note ?? "", section.title].join(" "))) continue;
        found = true;
        const inTitle = words.filter((word) => has(howTo.title, word)).length;
        scored.push({ match: { topic, section, howTo }, score: 10 + inTitle * 3, order: scored.length });
      }
      const statusText = (section.statuses ?? []).flatMap((kind) => {
        const group = statusGroup(kind);
        return [
          group.title,
          ...Object.keys(group.values).map((value) => `${value} ${describeStatus(kind, value) ?? ""}`),
        ];
      });
      if (!found && matches([topic.title, section.title, ...section.about, ...statusText].join(" "))) {
        const inTitle = words.filter((word) => has(section.title, word)).length;
        scored.push({ match: { topic, section }, score: inTitle * 3, order: scored.length });
      }
    }
  }
  return scored
    .sort((left, right) => right.score - left.score || left.order - right.order)
    .map((entry) => entry.match);
}
