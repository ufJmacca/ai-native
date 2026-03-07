# Task Management

## Problem

We need a simple but production ready tool that will allow the tracking of work items as they completed by different users. The work items should have a hierachy that starts at a Project level, has an Epic Level which tracks core feature/functionality blocks of a project, Stories that capture a user story related to an Epic, and Tasks that capture individual tasks required to complete a user story.

There should be a front end UI that allows users to manage tickets in both a Kanban style and also a sprint/backlog style. There should be a robust API backend that will allow easy interaction by agents and automations as well.

Each items should allow a title, description, estimated complexity points, assignee and reporter. individual ticket types could have additional fields that make sense for that ticket type. 

## Users

- Individual using the system to track their work
- Managers using the system to get status of projects, and work status

## Constraints

- Keep the first implementation intentionally small.
- Use tests before implementation changes.
- Document any assumptions directly in the PRD.

## Acceptance Criteria

- User front end web based ui allowing the creation, modification of tickets
- Front end web based UI that allows managing of tickets in a kanban board style
- Front end web based UI that allows managing of tickets in a sprints/backlog style
- Front end dashboard showing status of work completed vs. outstanding
- API backend allowing easy integration for agents and automations
- Includes automated tests for the API and UI behavior.
- Produces a README section explaining how to run the sample app.
