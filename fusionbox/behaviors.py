import operator
import copy
import datetime

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError, NON_FIELD_ERRORS
from django.db import models
from django.db.models.base import ModelBase
from django.db.models.query import QuerySet

from fusionbox.db.models import QuerySetManager


try:
    from django.contrib.admin.util import lookup_needs_distinct
except ImportError: 
    def lookup_needs_distinct(opts, lookup_path):
        """
        Returns True if 'distinct()' should be used to query the given lookup path.
        """
        field_name = lookup_path.split('__', 1)[0]
        field = opts.get_field_by_name(field_name)[0]
        if ((hasattr(field, 'rel') and
             isinstance(field.rel, models.ManyToManyRel)) or
            (isinstance(field, models.related.RelatedObject) and
             not field.field.unique)):
             return True
        return False


now = datetime.datetime.now

if getattr(settings, 'USE_TZ', False):
    # Django 1.3 does not have the django.utils.timezone module.
    try:
        from django.utils.timezone import utc
        now = lambda: datetime.datetime.utcnow().replace(tzinfo=utc)
    except ImportError:
        pass


class EmptyObject(object):
    def __nonzero__(self):
        return False


class MetaBehavior(ModelBase):
    """
    Base Metaclass for Behaviors
    """
    def __new__(cls, name, bases, attrs):
        """
        This allows declarative field definition in behaviors, just like in a
        regular model definition, while still allowing field names to be
        customized. Given a behavior::

            class FooBehavior(Behavior):
                some_column = IntegerField()

        A child class declaring::

            class MyModel(FooBehavior):
                class FooBehavior:
                    some_column = 'another_name'

        will be able to change the name of ``some_column`` to ``another_name``.

        To do this, we rip out all instances of :class:`model.Field`, and wait for
        :func:`Behavior.modify_schema` to add them back in once all config classes are
        merged.
        """
        found_django_meta_without_behavior = False
        for base in bases:
            if not issubclass(base, object):
                continue
            mro = base.mro()
            if found_django_meta_without_behavior and Behavior in mro:
                raise ImproperlyConfigured(u'Any model inheriting from a behavior cannot have a model which inherits from models.Model ahead of it in the parent classes')
            mro_modules = [klass.__module__ for klass in mro]
            if not 'fusionbox.behaviors' in mro_modules and models.Model in mro:
                found_django_meta_without_behavior = True

        declared_fields = {}

        if getattr(attrs.get('Meta', EmptyObject()), 'abstract', False):
            for property_name in attrs:
                if isinstance(attrs[property_name], models.Field):
                    declared_fields[property_name] = attrs[property_name]
            for field in declared_fields:
                del attrs[field]

        attrs['declared_fields'] = declared_fields

        new_class = super(MetaBehavior, cls).__new__(cls, name, bases, attrs)
        new_class.merge_parent_settings()
        if not new_class._meta.abstract:
            new_class.modify_schema()
        else:
            # make sure abstract classes have an inner settings class
            if not hasattr(new_class, new_class.__name__):
                setattr(new_class, new_class.__name__, EmptyObject())

        return new_class


class Behavior(models.Model):
    """
    Base class for all Behaviors

    Behaviors are implemented through model inheritance, and support
    multi-inheritance as well.  Each behavior adds a set of default fields
    and/or methods to the model.  Field names can be customized like example B.

    EXAMPLE A::

        class MyModel(FooBehavior):
            pass

    ``MyModel`` will have whatever fields ``FooBehavior`` adds with default
    field names.

    EXAMPLE B::

        class MyModel(FooBehavior):
            class FooBehavior:
                bar = 'qux'
                baz = 'quux'

    ``MyModel`` will have the fields from ``FooBehavior`` added, but the field
    names will be 'qux' and 'quux' respectively.

    EXAMPLE C::

        class MyModel(FooBehavior, BarBehavior):
            pass

    ``MyModel`` will have the fields from both ``FooBehavior`` and
    ``BarBehavior``, each with default field names.  To customizing field names
    can be done just like it was in example B.

    """
    class Meta:
        abstract = True
    __metaclass__ = MetaBehavior

    @classmethod
    def modify_schema(cls):
        """
        Hook for behaviors to modify their model class just after it's created
        """

        # Everything in declared_fields was pulled out by our metaclass, time
        # to add them back in
        for parent in cls.mro():
            if cls._meta.proxy:
                # Proxy models already had their fields added via the parent
                # model, so don't add them again.
                continue
            try:
                declared_fields = parent.declared_fields
            except AttributeError:  # Model itself doesn't have declared_fields
                continue

            for name, field in declared_fields.iteritems():
                if not hasattr(cls, parent.__name__):
                    setattr(cls, parent.__name__, EmptyObject())
                try:
                    new_name = getattr(getattr(cls, parent.__name__), name)
                except AttributeError:
                    new_name = name
                    # put the column name in the behavior's config, so it's always there
                    setattr(getattr(parent, parent.__name__), name, name)
                if not hasattr(cls, new_name):
                    cls.add_to_class(new_name, copy.copy(field))

    @classmethod
    def merge_parent_settings(cls):
        """
        Every behavior's settings are stored in an inner class whose name
        matches its behavior's name. This method implements inheritance for
        those inner classes.
        """
        behaviors = [behavior.__name__ for behavior in cls.base_behaviors()]
        for behavior in behaviors:
            parent_settings = [getattr(parent, behavior, False) for parent in cls.__bases__]
            if behavior in cls.__dict__:
                parent_settings = [getattr(cls, behavior)] + parent_settings
            parent_settings = filter(bool, parent_settings)
            if parent_settings:
                try:
                    setattr(cls, behavior, type(behavior, tuple(parent_settings), {}))
                except TypeError:
                    setattr(cls, behavior, type(behavior, tuple(parent_settings + [object]), {}))

    @classmethod
    def base_behaviors(cls):
        behaviors = []
        for parent in cls.mro():
            if hasattr(parent, parent.__name__):
                behaviors.append(parent)
        return behaviors


class QuerySetManagerModel(Behavior):
    """
    This behavior is meant to be used in conjunction with
    :class:`fusionbox.db.models.QuerySetManager`

    A class which inherits from this class will any inner QuerySet classes
    found in the `mro` merged into a single class.

    Given the following Parent class::

        class Parent(models.Model):
            class QuerySet(QuerySet):
                def get_active(self):
                    ...

    The following two Child classes are equivalent::

        class Child(Parent):
            class QuerySet(Parent.QuerySet):
                def get_inactive(self):
                    ...

        class Child(QuerySetManagerModel, Parent):
            class QuerySet(QuerySet):
                def get_inactive(self):
                    ...
    """

    objects = QuerySetManager()

    class QuerySet(QuerySet):
        pass

    class Meta:
        abstract = True

    @classmethod
    def merge_parent_settings(cls):
        """
        Automatically merges all parent QuerySet classes to preserve custom
        defined QuerySet methods
        """
        # get a list of all of the inner QuerySet classes from the bases
        querysets = [getattr(parent, 'QuerySet', False) for parent in cls.__bases__]
        # add in the inner QuerySet class defined on the child
        if 'QuerySet' in cls.__dict__:
            querysets = [cls.QuerySet] + querysets
        # remove False values from the the list.
        querysets = filter(bool, querysets)
        if querysets:
            # Create the new inner QuerySet class and put it on the new child.
            cls.QuerySet = type('QuerySet', tuple(querysets), {})
        # Conditional bailout since ManageQuerySet is not defined during it's instantiation
        if cls.__name__ == 'QuerySetManagerModel':
            return
        return super(QuerySetManagerModel, cls).merge_parent_settings()

# To preserve backwards compatability
ManagedQuerySet = QuerySetManagerModel


class Timestampable(Behavior):
    """
    Base class for adding timestamping behavior to a model.

    Added Fields:
        Field 1:
            field: DateTimeField(default=now)
            description: Timestamps set at the creation of the instance
            default_name: created_at
        Field 2:
            field: DateTimeField(auto_now=True)
            description: Timestamps set each time the save method is called on the instance
            default_name: updated_at

    """
    class Meta:
        abstract = True

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PublishableManager(models.Manager):
    """
    Manager for publishable behavior

    """
    def get_queryset(self):
        queryset = super(PublishableManager, self).get_queryset()
        return queryset.filter(is_published=True, publish_at__lte=now)


class Publishable(Behavior):
    """
    Base class for adding publishable behavior to a model.

    Added Fields:
        Field 1:
            field: DateTimeField(default=datetime.datetime.now, help_text='Selecting a future date will automatically publish to the live site on that date.')
            description: The date that the model instance will be made available to the PublishableManager's query set
            default_name: publish_at
        Field 2:
            field: DateTimeField(default=datetime.datetime.now, help_text='Selecting a future date will automatically publish to the live site on that date.')
            description: setting to False will automatically draft the instance, making it unavailable to the PublishableManager's query set
            default_name: is_published

    Added Managers:
        PublishableManager:
            description: overwritten get_queryset() function to only fetch published instances.
            name: published
            usage:
                class Blog(Publishable):
                ...

                all_blogs = Blog.objects.all()
                published_blogs = Blog.published.all()

    """
    class Meta:
        abstract = True

    publish_at = models.DateTimeField(default=now, help_text='Selecting a future date will automatically publish to the live site on that date.')
    is_published = models.BooleanField(default=True, help_text='Unchecking this will take the entry off the live site regardless of publishing date')

    objects = models.Manager()
    published = PublishableManager()


class SEO(Behavior):
    """
    Base class for adding seo behavior to a model.

    Added Fields:
        Field 1:
            field: CharField(max_length = 255)
            description: Char field intended for use in html <title> tag.
            validation: Max Length 255 Characters
            default_name: seo_title
        Field 2:
            field: TextField()
            description: Text field intended for use in html <meta name='description'> tag.
            default_name: seo_description
        Field 3:
            field: TextField()
            description: Text field intended for use in html <meta name='keywords'> tag.
            validation: comma separated text strings
            default_name: seo_keywords

    """
    class Meta:
        abstract = True

    seo_title = models.CharField(max_length=255)
    seo_description = models.TextField()
    seo_keywords = models.TextField()

    def formatted_seo_data(self, title='', description='', keywords=''):
        """
        A string containing the model's SEO data marked up and ready for output
        in HTML.
        """
        from django.utils.safestring import mark_safe
        from django.utils.html import escape

        escaped_data = tuple(map(escape,
            (getattr(self, self.SEO.seo_title, title),
             getattr(self, self.SEO.seo_description, description),
             getattr(self, self.SEO.seo_keywords, keywords))))
        return mark_safe('<title>%s</title>\n<meta name="description" content="%s"/>\n<meta name="keywords" content="%s"/>' % escaped_data)


class Validation(Behavior):
    """
    Base class for adding complex validation behavior to a model.

    By inheriting from Validation, your model can have ``validate`` and
    ``validate_<field>`` methods.

    :func:`validate` is for generic validations, and for ``NON_FIELD_ERRORS``, errors that do not belong to any
    one field.  In this method you can raise a ValidationError that contains a single error message, a list of
    errors, or - if the messages **are** associated with a field - a dictionary of field-names to message-list.

    You can also write ``validate_<field>`` methods for any columns that need custom validation.  This is for convience,
    since it is easier and more intuitive to raise an 'invalid value' from within one of these methods, and have it
    automatically associated with the correct field.

    Even if you don't implement custom validation methods, Validation changes the normal behavior of ``save`` so that
    validation **always** occurs.  This makes it easy to write APIs without having to understand the ``clean``, ``full_clean``,
    and :func:`clean_fields` methods that must called in django.  If a validation error occurs, the exception will **not** be
    caught, it is up to you to catch it in your view or API.

    """
    class Meta:
        abstract = True

    def clean_fields(self, exclude=None):
        """
        Must be manually called in Django.

        Calls any ``validate_<field>`` methods defined on the Model.
        """
        errors = {}
        try:
            super(Validation, self).clean_fields(exclude)
        except ValidationError, e:
            errors = e.update_error_dict(errors)

        # 'generic' validation.  you can raise a single error, a list of errors,
        # or a dictionary.
        try:
            if hasattr(self, 'validate'):
                getattr(self, 'validate')()
        except ValidationError, e:
            errors = e.update_error_dict(errors)

        # field validation.  Lists or a single message will be appended to the correct
        # entry.  Dictionaries will get merged in.
        for f in self._meta.fields:
            try:
                if hasattr(self, 'validate_' + f.name):
                    getattr(self, 'validate_' + f.name)()
            except ValidationError, e:
                if hasattr(e, 'message_dict'):
                    for k, v in e.message_dict.items():
                        errors.setdefault(k, []).extend(v)
                else:
                    errors.setdefault(f.name, []).extend(e.messages)

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super(Validation, self).save(*args, **kwargs)

    def is_valid(self):
        """
        Returns ``True`` or ``False``
        """
        return not self.validation_errors()

    def validation_errors(self):
        """
        Returns a dictionary of errors.
        """
        try:
            self.full_clean()
            return {}
        except ValidationError, e:
            if hasattr(e, 'message_dict'):
                return e.message_dict
            return {NON_FIELD_ERRORS: e.messages}


def construct_search(field_name):
    if field_name.startswith('^'):
        return "%s__istartswith" % field_name[1:]
    elif field_name.startswith('='):
        return "%s__iexact" % field_name[1:]
    elif field_name.startswith('@'):
        return "%s__search" % field_name[1:]
    else:
        return "%s__icontains" % field_name


class AdminSearchableQueryset(models.query.QuerySet):
    def search(self, query):
        orm_lookups = [construct_search(str(search_field))
                       for search_field in self.search_fields]
        for bit in query.split():
            or_queries = [models.Q(**{orm_lookup: bit})
                          for orm_lookup in orm_lookups]
            self = self.filter(reduce(operator.or_, or_queries))

        for search_spec in orm_lookups:
            if lookup_needs_distinct(self.model._meta, search_spec):
                self = self.distinct()
                break

        return self
