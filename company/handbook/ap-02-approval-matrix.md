# AP-02 Approval matrix

Purpose: who must approve a vendor invoice before it is posted, and how approvals work for the AI employee.

## AP-001 — Invoice approval limit

- Total payable up to and including ₹50,000: AP may post directly.
- Total payable above ₹50,000: the Finance Manager (Neha Kulkarni, role `finance_manager`) must approve before posting.

The limit applies to the invoice total including GST. An approval covers one specific invoice and amount, once.

## How approvals work for the AI employee

When the AI employee submits an invoice above the limit, the system holds the submission and creates an approval request showing the vendor, invoice number, amount and rule. Work on that invoice pauses until the approver decides. If it is approved, the AI employee submits the identical invoice once more. If it is rejected, nothing is posted and the rejection reason is reported to the requester.

## Other approvals

- Admin-level system access: the IT Admin (Vikram Desai).
- New vendors: procurement onboarding (see ap-03), not AP.
