from django import template
from django.template.loader import render_to_string

register = template.Library()


@register.simple_tag(takes_context=True, name='uncaptcha')
def uncaptcha(context, form=None):
    """
    Renders the uncaptcha field for a form.
    """
    if form:
        field = form['uncaptcha']
    else:
        field = context['form']['uncaptcha']

    context['field'] = field

    rendered_uncaptcha = render_to_string('forms/fields/uncaptcha.html', context)
    del context['field']
    return rendered_uncaptcha
