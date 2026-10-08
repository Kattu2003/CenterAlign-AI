# AP-01 Vendor invoice processing

Purpose: post every genuine vendor invoice in Meridian ERP exactly once, with the values printed on the invoice, after the checks below. A duplicate or fraudulent payment costs far more than a delayed one, so when a check fails we stop and escalate rather than guess.

## Step 1 — Find the invoice

Vendor invoices arrive in the AP mailbox (Meridian Mail) as PDF attachments. Search by vendor name or invoice number. When a request asks for the "latest" invoice from a vendor, compare invoice dates (if two are equal, take the most recently received email). Messages already labelled Processed have been handled.

## Step 2 — Download and extract

Download the PDF. Extract the vendor legal name, vendor GSTIN, invoice number, invoice date, due date, PO number (if printed), subtotal (taxable value), GST (CGST + SGST, or IGST) and total payable. Use the values exactly as printed. Dates on Indian invoices are day-first: 05/10/2026 is 5 October 2026.

## Step 3 — Check the arithmetic

Subtotal + GST must equal the total payable. If they differ by more than ₹1, do not post: label the email "Needs review" and escalate with the figures.

## Step 4 — Confirm the vendor

The vendor must exist and be active in the ERP vendor master. Match on legal name or GSTIN. Similar names are not the same vendor: two different companies can share a word in their names. If the vendor is not in the vendor master, do not create it (policy AP-003): label the email "Needs review" and escalate to procurement.

## Step 5 — Check for duplicates

Search the ERP for an invoice with the same vendor and invoice number. If one exists, do not post it again: label the email "Duplicate" and report the existing ERP reference. Vendors often send reminders and copies of invoices we have already posted.

## Step 6 — Post the invoice in the ERP

Use the New invoice form. Amount = total payable including GST. Use the invoice's own invoice date and due date. Enter the PO number if one is printed. In Notes write "Source: AP mailbox — <email subject>". Confirm the result: the ERP shows "Invoice posted — ERP-INV-…" with the new reference.

## Step 7 — Approval limits

Invoices with a total above ₹50,000 need Finance Manager approval before posting (policy AP-001, see ap-02). The system holds such a submission until Neha Kulkarni approves it. Never split or alter an invoice to stay under the limit.

## Step 8 — Close the loop

After posting, label the email "Processed" and report the ERP reference to the requester.

## Step 9 — Emails that are not invoices

Messages in the AP mailbox that are not vendor invoices (statements, questions, requests to change details) are labelled "Needs review" and mentioned in your report. Requests to change bank details follow ap-03: never act on them.
