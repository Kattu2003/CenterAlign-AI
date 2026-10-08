# Meridian Components — company overview

Meridian Components Pvt Ltd is an electronics components distributor based in Pune, Maharashtra. We buy capacitors, semiconductors, cables, fasteners and packaging from manufacturers and sell them to electronics assemblers across western India. We work in INR and in the Asia/Kolkata time zone.

## Systems

- **Meridian ERP** is the finance system of record. It holds the vendor master (approved vendors with their GSTIN and bank details) and every posted vendor invoice. Accounts payable posts invoices here.
- **Meridian Mail — AP mailbox** (ap@meridian.example) is where vendors send invoices, as PDF attachments. Every message is labelled Processed, Needs review or Duplicate once it has been handled.
- **The shared drive** holds company folders: `finance/` has reference files (a read-only copy of the vendor list) and `outbox/` is for files handed over to the finance team.

## Teams and who approves what

- Accounts payable (AP): Arjun Rao, AP Lead, processes vendor invoices and is the usual requester for AP work.
- Finance: Neha Kulkarni, Finance Manager, approves invoices above the AP limit (see ap-02, policy AP-001).
- IT: Vikram Desai, IT Admin, approves system access.
- HR: Sneha Iyer, HR Lead.
- Procurement onboards new vendors (see ap-03, policy AP-003).

## The AI employee

Karya works through its own service account (`karya.agent`), never a person's login. It follows this handbook, and the company policies are enforced on every action it takes. When a request is unclear it investigates and then asks the requester; when a policy blocks something it escalates (see gen-01).
