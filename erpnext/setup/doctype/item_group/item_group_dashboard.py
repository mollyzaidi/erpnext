from frappe import _


def get_data():
	return {
		'fieldname': 'item_group',
		'transactions': [
			{
				'label': _('Items'),
				'items': ['Item']
			},
			{
				'label': _('Configuration'),
				'items': ['Item Default Rule']
			},
		]
	}
