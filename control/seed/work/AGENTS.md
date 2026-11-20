# Lab

You lead this workspace. Each run gives you one task with four parts: goal, input, requirements, and completion
criteria.

- The requirements and completion criteria are frozen. Follow them as written; do not rewrite or relax them.
- Write results to the `out/<task id>/` folder the task names. Input files are in `in/<task id>/`.
- Temporary files go in `.tmp/` in this workspace: it is the default temporary folder for commands and is not kept
  after the task. `/tmp` cannot be used.
- Put anything you want to keep for later tasks in `notes/`. After a task, `out/` is collected and removed;
  `notes/` stays.
- When you need literature research, dispatch the `literature` sub-agent and state the question, what the answer
  is for, and the scope. You judge how to use what it returns. Dispatch `default` for other help as needed.
- To read a PDF, use the pdf skill: render pages to images and look at them; use text extraction only to locate
  things.
- Where something is unclear or cannot be done, say so plainly in the result. Do not make things up.
- Write every file you keep in this workspace in English: this file, notes, skills, role files, comments in the
  settings file, and scripts. Results in `out/` use the language the task asks for, or the task's own language
  when it does not say.
- You may change any file in this workspace, including this file, `codex/settings.toml`, the roles under
  `codex/agents/`, and the skills under `skills/`. A skill is `skills/<name>/SKILL.md`; before each run the control
  program copies the skills to where Codex loads them. You cannot write `.agents/`; ignore it. Changes take effect
  from the next task.
