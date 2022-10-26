# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe, json
from frappe.utils import cstr, cint, get_fullname
from frappe import _
from frappe.model.mapper import get_mapped_doc
from erpnext.setup.utils import get_exchange_rate
from erpnext.utilities.transaction_base import TransactionBase
from erpnext.accounts.party import get_party_account_currency
from frappe.email.inbox import link_communication_to_document

subject_field = "title"
sender_field = "contact_email"


class Opportunity(TransactionBase):
	def __init__(self, *args, **kwargs):
		super(Opportunity, self).__init__(*args, **kwargs)
		self.status_map = [
			["Lost", "eval:self.status=='Lost'"],
			["Lost", "has_lost_quotation"],
			["Quotation", "has_active_quotation"],
			["Converted", "has_ordered_quotation"],
			["Closed", "eval:self.status=='Closed'"]
		]

	def validate(self):
		self.validate_item_details()
		self.validate_cust_name()
		self.validate_uom_is_integer("uom", "qty")

		if not self.title:
			self.title = self.customer_name

	def after_insert(self):
		if self.opportunity_from == "Lead":
			frappe.get_doc("Lead", self.party_name).set_status(update=True)

	def on_trash(self):
		self.delete_events()

	def declare_enquiry_lost(self, lost_reasons_list, detailed_reason=None):
		if not self.has_active_quotation():
			frappe.db.set(self, 'status', 'Lost')

			if detailed_reason:
				frappe.db.set(self, 'order_lost_reason', detailed_reason)

			for reason in lost_reasons_list:
				self.append('lost_reasons', reason)

			self.save()

		else:
			frappe.throw(_("Cannot declare as lost, because Quotation has been made."))

	def has_active_quotation(self):
		vehicle_quotation = frappe.db.get_value("Vehicle Quotation", {
			"opportunity": self.name,
			"docstatus": 1,
			"status": ("not in", ['Lost', 'Closed'])
		})

		quotation = frappe.get_all('Quotation', {
			'opportunity': self.name,
			'status': ("not in", ['Lost', 'Closed']),
			'docstatus': 1
		}, 'name')

		return quotation or vehicle_quotation

	def has_ordered_quotation(self):
		vehicle_quotation = frappe.db.get_value("Vehicle Quotation", {
			"opportunity": self.name,
			"docstatus": 1,
			"status": 'Ordered'
		})

		quotation = frappe.db.sql("""
			select q.name
			from `tabQuotation` q, `tabQuotation Item` qi
			where q.name = qi.parent and q.docstatus=1 and qi.prevdoc_docname =%s
			and q.status = 'Ordered'""", self.name)

		return quotation or vehicle_quotation

	def has_lost_quotation(self):
		vehicle_quotation = frappe.db.get_value("Vehicle Quotation", {
			"opportunity": self.name,
			"docstatus": 1,
			"status": 'Lost'
		})

		lost_quotation = frappe.db.sql("""
			select name
			from `tabQuotation`
			where  docstatus=1
				and opportunity =%s
				and status = 'Lost'
			""", self.name)

		if lost_quotation or vehicle_quotation:
			if self.has_active_quotation():
				return False
			return True

	def validate_cust_name(self):
		if self.party_name and self.opportunity_from == 'Customer':
			self.customer_name = frappe.db.get_value("Customer", self.party_name, "customer_name")
		elif self.party_name and self.opportunity_from == 'Lead':
			lead_name, company_name = frappe.db.get_value("Lead", self.party_name, ["lead_name", "company_name"])
			self.customer_name = company_name or lead_name

	def validate_item_details(self):
		if not self.get('items'):
			return

		# set missing values
		item_fields = ("item_name", "description", "item_group", "brand")

		for d in self.items:
			if not d.item_code:
				continue

			item = frappe.db.get_value("Item", d.item_code, item_fields, as_dict=True)
			for key in item_fields:
				if not d.get(key): d.set(key, item.get(key))


@frappe.whitelist()
def get_item_details(item_code):
	item_details = frappe.db.sql("""
		select item_name, stock_uom, image, description, item_group, brand
		from `tabItem`
		where name = %s
	""", item_code, as_dict=1)

	item_details = item_details[0] if item_details else frappe._dict()

	return {
		'item_name': item.item_name,
		'description': item.description,
		'uom': item.stock_uom,
		'image': item.image,
		'item_group': item.item_group,
		'brand': item.brand,
	}


@frappe.whitelist()
def make_quotation(source_name, target_doc=None):
	def set_missing_values(source, target):
		from erpnext.controllers.accounts_controller import get_default_taxes_and_charges
		quotation = frappe.get_doc(target)

		company_currency = frappe.get_cached_value('Company',  quotation.company,  "default_currency")

		if quotation.quotation_to == 'Customer' and quotation.party_name:
			party_account_currency = get_party_account_currency("Customer", quotation.party_name, quotation.company)
		else:
			party_account_currency = company_currency

		quotation.currency = party_account_currency or company_currency

		if company_currency == quotation.currency:
			exchange_rate = 1
		else:
			exchange_rate = get_exchange_rate(quotation.currency, company_currency,
				quotation.transaction_date, args="for_selling")

		quotation.conversion_rate = exchange_rate

		# get default taxes
		taxes = get_default_taxes_and_charges("Sales Taxes and Charges Template", company=quotation.company)
		if taxes.get('taxes'):
			quotation.update(taxes)

		quotation.opportunity = source.name

		quotation.run_method("set_missing_values")
		quotation.run_method("calculate_taxes_and_totals")

	doclist = get_mapped_doc("Opportunity", source_name, {
		"Opportunity": {
			"doctype": "Quotation",
			"field_map": {
				"opportunity_from": "quotation_to",
				"opportunity_type": "order_type",
				"name": "enq_no",
			}
		},
		"Opportunity Item": {
			"doctype": "Quotation Item",
			"field_map": {
				"parent": "prevdoc_docname",
				"parenttype": "prevdoc_doctype",
				"uom": "stock_uom"
			},
			"add_if_empty": True
		}
	}, target_doc, set_missing_values)

	return doclist


@frappe.whitelist()
def make_request_for_quotation(source_name, target_doc=None):
	doclist = get_mapped_doc("Opportunity", source_name, {
		"Opportunity": {
			"doctype": "Request for Quotation"
		},
		"Opportunity Item": {
			"doctype": "Request for Quotation Item",
			"field_map": [
				["name", "opportunity_item"],
				["parent", "opportunity"],
				["uom", "uom"]
			]
		}
	}, target_doc)

	return doclist


@frappe.whitelist()
def make_supplier_quotation(source_name, target_doc=None):
	doclist = get_mapped_doc("Opportunity", source_name, {
		"Opportunity": {
			"doctype": "Supplier Quotation",
			"field_map": {
				"name": "opportunity"
			}
		},
		"Opportunity Item": {
			"doctype": "Supplier Quotation Item",
			"field_map": {
				"uom": "stock_uom"
			}
		}
	}, target_doc)

	return doclist


@frappe.whitelist()
def set_multiple_status(names, status):
	names = json.loads(names)
	for name in names:
		opp = frappe.get_doc("Opportunity", name)
		opp.status = status
		opp.save()


def auto_close_opportunity():
	""" auto close the `Replied` Opportunities after 7 days """
	auto_close_after_days = frappe.db.get_single_value("Selling Settings", "close_opportunity_after_days") or 15

	opportunities = frappe.db.sql(""" select name from tabOpportunity where status='Replied' and
		modified<DATE_SUB(CURDATE(), INTERVAL %s DAY) """, (auto_close_after_days), as_dict=True)

	for opportunity in opportunities:
		doc = frappe.get_doc("Opportunity", opportunity.get("name"))
		doc.status = "Closed"
		doc.flags.ignore_permissions = True
		doc.flags.ignore_mandatory = True
		doc.save()


@frappe.whitelist()
def make_opportunity_from_communication(communication, company, ignore_communication_links=False):
	from erpnext.crm.doctype.lead.lead import make_lead_from_communication
	doc = frappe.get_doc("Communication", communication)

	lead = doc.reference_name if doc.reference_doctype == "Lead" else None
	if not lead:
		lead = make_lead_from_communication(communication, ignore_communication_links=True)

	opportunity_from = "Lead"

	opportunity = frappe.get_doc({
		"doctype": "Opportunity",
		"company": company,
		"opportunity_from": opportunity_from,
		"party_name": lead
	}).insert(ignore_permissions=True)

	link_communication_to_document(doc, "Opportunity", opportunity.name, ignore_communication_links)

	return opportunity.name
