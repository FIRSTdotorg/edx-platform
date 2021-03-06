"""
Test grade calculation.
"""

import ddt
from django.http import Http404
import itertools
from mock import patch
from nose.plugins.attrib import attr
from opaque_keys.edx.locations import SlashSeparatedCourseKey

from capa.tests.response_xml_factory import MultipleChoiceResponseXMLFactory
from courseware.tests.helpers import get_request_for_user
from lms.djangoapps.course_blocks.api import get_course_blocks
from student.tests.factories import UserFactory
from student.models import CourseEnrollment
from xmodule.block_metadata_utils import display_name_with_default_escaped
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.tests.factories import CourseFactory, ItemFactory
from xmodule.modulestore.tests.django_utils import SharedModuleStoreTestCase
from xmodule.graders import ProblemScore

from .utils import answer_problem
from .. import course_grades
from ..course_grades import summary as grades_summary
from ..new.course_grade import CourseGradeFactory
from ..new.subsection_grade import SubsectionGradeFactory


def _grade_with_errors(student, course):
    """This fake grade method will throw exceptions for student3 and
    student4, but allow any other students to go through normal grading.

    It's meant to simulate when something goes really wrong while trying to
    grade a particular student, so we can test that we won't kill the entire
    course grading run.
    """
    if student.username in ['student3', 'student4']:
        raise Exception("I don't like {}".format(student.username))

    return grades_summary(student, course)


def _create_problem_xml():
    """
    Creates and returns XML for a multiple choice response problem
    """
    return MultipleChoiceResponseXMLFactory().build_xml(
        question_text='The correct answer is Choice 3',
        choices=[False, False, True, False],
        choice_names=['choice_0', 'choice_1', 'choice_2', 'choice_3']
    )


@attr(shard=1)
class TestGradeIteration(SharedModuleStoreTestCase):
    """
    Test iteration through student gradesets.
    """
    COURSE_NUM = "1000"
    COURSE_NAME = "grading_test_course"

    @classmethod
    def setUpClass(cls):
        super(TestGradeIteration, cls).setUpClass()
        cls.course = CourseFactory.create(
            display_name=cls.COURSE_NAME,
            number=cls.COURSE_NUM
        )

    def setUp(self):
        """
        Create a course and a handful of users to assign grades
        """
        super(TestGradeIteration, self).setUp()

        self.students = [
            UserFactory.create(username='student1'),
            UserFactory.create(username='student2'),
            UserFactory.create(username='student3'),
            UserFactory.create(username='student4'),
            UserFactory.create(username='student5'),
        ]

    def test_empty_student_list(self):
        """If we don't pass in any students, it should return a zero-length
        iterator, but it shouldn't error."""
        gradeset_results = list(course_grades.iterate_grades_for(self.course.id, []))
        self.assertEqual(gradeset_results, [])

    def test_nonexistent_course(self):
        """If the course we want to get grades for does not exist, a `Http404`
        should be raised. This is a horrible crossing of abstraction boundaries
        and should be fixed, but for now we're just testing the behavior. :-("""
        with self.assertRaises(Http404):
            gradeset_results = course_grades.iterate_grades_for(SlashSeparatedCourseKey("I", "dont", "exist"), [])
            gradeset_results.next()

    def test_all_empty_grades(self):
        """No students have grade entries"""
        all_gradesets, all_errors = self._gradesets_and_errors_for(self.course.id, self.students)
        self.assertEqual(len(all_errors), 0)
        for gradeset in all_gradesets.values():
            self.assertIsNone(gradeset['grade'])
            self.assertEqual(gradeset['percent'], 0.0)

    @patch('lms.djangoapps.grades.course_grades.summary', _grade_with_errors)
    def test_grading_exception(self):
        """Test that we correctly capture exception messages that bubble up from
        grading. Note that we only see errors at this level if the grading
        process for this student fails entirely due to an unexpected event --
        having errors in the problem sets will not trigger this.

        We patch the grade() method with our own, which will generate the errors
        for student3 and student4.
        """
        all_gradesets, all_errors = self._gradesets_and_errors_for(self.course.id, self.students)
        student1, student2, student3, student4, student5 = self.students
        self.assertEqual(
            all_errors,
            {
                student3: "I don't like student3",
                student4: "I don't like student4"
            }
        )

        # But we should still have five gradesets
        self.assertEqual(len(all_gradesets), 5)

        # Even though two will simply be empty
        self.assertFalse(all_gradesets[student3])
        self.assertFalse(all_gradesets[student4])

        # The rest will have grade information in them
        self.assertTrue(all_gradesets[student1])
        self.assertTrue(all_gradesets[student2])
        self.assertTrue(all_gradesets[student5])

    ################################# Helpers #################################
    def _gradesets_and_errors_for(self, course_id, students):
        """Simple helper method to iterate through student grades and give us
        two dictionaries -- one that has all students and their respective
        gradesets, and one that has only students that could not be graded and
        their respective error messages."""
        students_to_gradesets = {}
        students_to_errors = {}

        for student, gradeset, err_msg in course_grades.iterate_grades_for(course_id, students):
            students_to_gradesets[student] = gradeset
            if err_msg:
                students_to_errors[student] = err_msg

        return students_to_gradesets, students_to_errors


@ddt.ddt
class TestWeightedProblems(SharedModuleStoreTestCase):
    """
    Test scores and grades with various problem weight values.
    """
    @classmethod
    def setUpClass(cls):
        super(TestWeightedProblems, cls).setUpClass()
        cls.course = CourseFactory.create()
        cls.chapter = ItemFactory.create(parent=cls.course, category="chapter", display_name="chapter")
        cls.sequential = ItemFactory.create(parent=cls.chapter, category="sequential", display_name="sequential")
        cls.vertical = ItemFactory.create(parent=cls.sequential, category="vertical", display_name="vertical1")
        problem_xml = _create_problem_xml()
        cls.problems = []
        for i in range(2):
            cls.problems.append(
                ItemFactory.create(
                    parent=cls.vertical,
                    category="problem",
                    display_name="problem_{}".format(i),
                    data=problem_xml,
                )
            )

    def setUp(self):
        super(TestWeightedProblems, self).setUp()
        self.user = UserFactory()
        self.request = get_request_for_user(self.user)

    def _verify_grades(self, raw_earned, raw_possible, weight, expected_score):
        """
        Verifies the computed grades are as expected.
        """
        with self.store.branch_setting(ModuleStoreEnum.Branch.draft_preferred):
            # pylint: disable=no-member
            for problem in self.problems:
                problem.weight = weight
                self.store.update_item(problem, self.user.id)
            self.store.publish(self.course.location, self.user.id)

        course_structure = get_course_blocks(self.request.user, self.course.location)

        # answer all problems
        for problem in self.problems:
            answer_problem(self.course, self.request, problem, score=raw_earned, max_value=raw_possible)

        # get grade
        subsection_grade = SubsectionGradeFactory(
            self.request.user, self.course, course_structure
        ).update(self.sequential)

        # verify all problem grades
        for problem in self.problems:
            problem_score = subsection_grade.locations_to_scores[problem.location]
            expected_score.display_name = display_name_with_default_escaped(problem)
            expected_score.module_id = problem.location
            self.assertEquals(problem_score, expected_score)

        # verify subsection grades
        self.assertEquals(subsection_grade.all_total.earned, expected_score.earned * len(self.problems))
        self.assertEquals(subsection_grade.all_total.possible, expected_score.possible * len(self.problems))

    @ddt.data(
        *itertools.product(
            (0.0, 0.5, 1.0, 2.0),  # raw_earned
            (-2.0, -1.0, 0.0, 0.5, 1.0, 2.0),  # raw_possible
            (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 50.0, None),  # weight
        )
    )
    @ddt.unpack
    def test_problem_weight(self, raw_earned, raw_possible, weight):

        use_weight = weight is not None and raw_possible != 0
        if use_weight:
            expected_w_earned = raw_earned / raw_possible * weight
            expected_w_possible = weight
        else:
            expected_w_earned = raw_earned
            expected_w_possible = raw_possible

        expected_graded = expected_w_possible > 0

        expected_score = ProblemScore(
            raw_earned=raw_earned,
            raw_possible=raw_possible,
            weighted_earned=expected_w_earned,
            weighted_possible=expected_w_possible,
            weight=weight,
            graded=expected_graded,
            display_name=None,  # problem-specific, filled in by _verify_grades
            module_id=None,  # problem-specific, filled in by _verify_grades
        )
        self._verify_grades(raw_earned, raw_possible, weight, expected_score)


class TestScoreForModule(SharedModuleStoreTestCase):
    """
    Test the method that calculates the score for a given block based on the
    cumulative scores of its children. This test class uses a hard-coded block
    hierarchy with scores as follows:
                                                a
                                       +--------+--------+
                                       b                 c
                        +--------------+-----------+     |
                        d              e           f     g
                     +-----+     +-----+-----+     |     |
                     h     i     j     k     l     m     n
                   (2/5) (3/5) (0/1)   -   (1/3)   -   (3/10)

    """
    @classmethod
    def setUpClass(cls):
        super(TestScoreForModule, cls).setUpClass()
        cls.course = CourseFactory.create()
        cls.a = ItemFactory.create(parent=cls.course, category="chapter", display_name="a")
        cls.b = ItemFactory.create(parent=cls.a, category="sequential", display_name="b")
        cls.c = ItemFactory.create(parent=cls.a, category="sequential", display_name="c")
        cls.d = ItemFactory.create(parent=cls.b, category="vertical", display_name="d")
        cls.e = ItemFactory.create(parent=cls.b, category="vertical", display_name="e")
        cls.f = ItemFactory.create(parent=cls.b, category="vertical", display_name="f")
        cls.g = ItemFactory.create(parent=cls.c, category="vertical", display_name="g")
        cls.h = ItemFactory.create(parent=cls.d, category="problem", display_name="h")
        cls.i = ItemFactory.create(parent=cls.d, category="problem", display_name="i")
        cls.j = ItemFactory.create(parent=cls.e, category="problem", display_name="j")
        cls.k = ItemFactory.create(parent=cls.e, category="html", display_name="k")
        cls.l = ItemFactory.create(parent=cls.e, category="problem", display_name="l")
        cls.m = ItemFactory.create(parent=cls.f, category="html", display_name="m")
        cls.n = ItemFactory.create(parent=cls.g, category="problem", display_name="n")

        cls.request = get_request_for_user(UserFactory())
        CourseEnrollment.enroll(cls.request.user, cls.course.id)

        answer_problem(cls.course, cls.request, cls.h, score=2, max_value=5)
        answer_problem(cls.course, cls.request, cls.i, score=3, max_value=5)
        answer_problem(cls.course, cls.request, cls.j, score=0, max_value=1)
        answer_problem(cls.course, cls.request, cls.l, score=1, max_value=3)
        answer_problem(cls.course, cls.request, cls.n, score=3, max_value=10)

        cls.course_grade = CourseGradeFactory(cls.request.user).create(cls.course)

    def test_score_chapter(self):
        earned, possible = self.course_grade.score_for_module(self.a.location)
        self.assertEqual(earned, 9)
        self.assertEqual(possible, 24)

    def test_score_section_many_leaves(self):
        earned, possible = self.course_grade.score_for_module(self.b.location)
        self.assertEqual(earned, 6)
        self.assertEqual(possible, 14)

    def test_score_section_one_leaf(self):
        earned, possible = self.course_grade.score_for_module(self.c.location)
        self.assertEqual(earned, 3)
        self.assertEqual(possible, 10)

    def test_score_vertical_two_leaves(self):
        earned, possible = self.course_grade.score_for_module(self.d.location)
        self.assertEqual(earned, 5)
        self.assertEqual(possible, 10)

    def test_score_vertical_two_leaves_one_unscored(self):
        earned, possible = self.course_grade.score_for_module(self.e.location)
        self.assertEqual(earned, 1)
        self.assertEqual(possible, 4)

    def test_score_vertical_no_score(self):
        earned, possible = self.course_grade.score_for_module(self.f.location)
        self.assertEqual(earned, 0)
        self.assertEqual(possible, 0)

    def test_score_vertical_one_leaf(self):
        earned, possible = self.course_grade.score_for_module(self.g.location)
        self.assertEqual(earned, 3)
        self.assertEqual(possible, 10)

    def test_score_leaf(self):
        earned, possible = self.course_grade.score_for_module(self.h.location)
        self.assertEqual(earned, 2)
        self.assertEqual(possible, 5)

    def test_score_leaf_no_score(self):
        earned, possible = self.course_grade.score_for_module(self.m.location)
        self.assertEqual(earned, 0)
        self.assertEqual(possible, 0)
